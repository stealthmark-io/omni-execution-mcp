"""Comprehensive Hyperliquid SDK Parity Tests.

Closes and locks in all 4 Hyperliquid SDK parity dimensions:
1. triggerPx wire normalization (float -> str) + SDK canonical key order ('isMarket', 'triggerPx', 'tpsl')
2. order_type='market' and malformed order_type clean ValueError with zero /info network calls
3. NaN / inf / -inf / bool clean ValueError for px, sz, and triggerPx with zero /info network calls
4. spotMeta.tokens out-of-order index resolution (e.g. HYPE -> 10107)

Structure:
- HyperliquidParityDeterministicTests: Runs in stdlib Python (zero external dependencies, 0 skips).
- HyperliquidParitySDKTests: Port of Claude's 28+ test check_fix battery against hyperliquid-python-sdk 0.24
  (skipped if hyperliquid-python-sdk is not installed in the active interpreter).
"""

from __future__ import annotations

import copy
import json
import math
import time
import unittest
from decimal import Decimal
from typing import Any, Dict, List, Tuple

from omni_execution_mcp import rails
from omni_execution_mcp.config import OmniFeeConfig

# Detect Hyperliquid SDK availability in this interpreter
try:
    import msgpack
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    from hyperliquid import api as hl_api
    from hyperliquid.info import Info
    from hyperliquid.utils import signing
    from hyperliquid.utils.types import Cloid

    HAS_HL_SDK = True
except ImportError:
    HAS_HL_SDK = False


# Shared fixture data mirroring Hyperliquid Mainnet Info state
TEST_BUILDER = "0x8c967E73E7B15087c42A10D344cFf4c96D877f1D"
TEST_CLOID = "0x" + "0123456789abcdef" * 2
TEST_NONCE = 1_762_300_000_000

FIXTURE_META = {
    "universe": [
        {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
        {"name": "ETH", "szDecimals": 4, "maxLeverage": 25},
        {"name": "OLD", "szDecimals": 2, "maxLeverage": 3, "isDelisted": True},
        {"name": "SOL", "szDecimals": 2, "maxLeverage": 20},
        {"name": "kPEPE", "szDecimals": 0, "maxLeverage": 10},
        {"name": "DOGE", "szDecimals": 0, "maxLeverage": 10},
    ]
}

FIXTURE_SPOT_META = {
    "tokens": [
        {"name": "USDC", "szDecimals": 8, "weiDecimals": 8, "index": 0},
        {"name": "PURR", "szDecimals": 0, "weiDecimals": 5, "index": 1},
        {"name": "HFUN", "szDecimals": 2, "weiDecimals": 8, "index": 2},
        {"name": "HYPE", "szDecimals": 2, "weiDecimals": 8, "index": 3},
    ],
    "universe": [
        {"name": "PURR/USDC", "tokens": [1, 0], "index": 0, "isCanonical": True},
        {"name": "@1", "tokens": [2, 0], "index": 1, "isCanonical": False},
        {"name": "@107", "tokens": [3, 0], "index": 107, "isCanonical": False},
    ],
}


class MockTransport:
    """Deterministic mock transport tracking calls and answering /info requests."""

    def __init__(self, meta: Dict[str, Any] = FIXTURE_META, spot_meta: Dict[str, Any] = FIXTURE_SPOT_META):
        self.meta = meta
        self.spot_meta = spot_meta
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def __call__(self, url: str, body: bytes) -> bytes:
        req = json.loads(body.decode("utf-8"))
        self.calls.append((url, req))
        req_type = req.get("type")
        if req_type == "spotMeta":
            return json.dumps(self.spot_meta).encode("utf-8")
        elif req_type == "meta":
            return json.dumps(self.meta).encode("utf-8")
        raise AssertionError(f"Unexpected transport call {url} {req}")

    @property
    def call_count(self) -> int:
        return len(self.calls)


def _make_config(tenths: int = 30, active: bool = True) -> OmniFeeConfig:
    return OmniFeeConfig(
        hyperliquid_builder_address=TEST_BUILDER if active else None,
        hyperliquid_builder_tenths_bps=tenths,
    )


# ============================================================================
# Test Suite 1: Pure Standard-Library Parity (Zero External Dependencies)
# ============================================================================
class HyperliquidParityDeterministicTests(unittest.TestCase):
    """Deterministic tests for Gaps 1-4 that always run in stdlib Python."""

    def setUp(self):
        self.transport = MockTransport()
        self.config = _make_config(30)

    def test_gap1_trigger_px_wire_normalization_and_key_order(self):
        """Gap 1: triggerPx float is normalized to string and keys strictly follow SDK order."""
        trigger_sdk = {"trigger": {"isMarket": True, "triggerPx": 90000.0, "tpsl": "sl"}}
        payload = rails.build_hyperliquid_order_payload(
            coin="BTC",
            is_buy=False,
            sz=0.001,
            px=90000,
            order_type=copy.deepcopy(trigger_sdk),
            reduce_only=True,
            config=self.config,
            transport=self.transport,
        )
        t = payload["action"]["orders"][0]["t"]
        self.assertIn("trigger", t)
        trigger = t["trigger"]

        # Exact canonical key order: isMarket, triggerPx, tpsl
        self.assertEqual(list(trigger.keys()), ["isMarket", "triggerPx", "tpsl"])
        # String normalization of triggerPx
        self.assertEqual(trigger["triggerPx"], "90000")
        self.assertTrue(trigger["isMarket"])
        self.assertEqual(trigger["tpsl"], "sl")

        # Also test with float decimals and wire-string passthrough
        t2 = rails._hyperliquid_order_type_to_wire(
            {"trigger": {"isMarket": False, "triggerPx": 1234.56, "tpsl": "tp"}}
        )
        self.assertEqual(t2["trigger"]["triggerPx"], "1234.56")
        self.assertEqual(list(t2["trigger"].keys()), ["isMarket", "triggerPx", "tpsl"])

        t3 = rails._hyperliquid_order_type_to_wire(
            {"trigger": {"isMarket": True, "triggerPx": "90000", "tpsl": "sl"}}
        )
        self.assertEqual(t3["trigger"]["triggerPx"], "90000")

    def test_gap2_order_type_market_and_malformed_rejected_zero_network(self):
        """Gap 2: order_type='market' and malformed dicts raise ValueError before network calls."""
        # 'market' string must be rejected with clean ValueError
        with self.assertRaises(ValueError) as ctx:
            rails.build_hyperliquid_order_payload(
                coin="BTC",
                is_buy=True,
                sz=0.001,
                px=97000,
                order_type="market",
                config=self.config,
                transport=self.transport,
            )
        self.assertIn("Unsupported Hyperliquid order_type 'market'", str(ctx.exception))
        self.assertEqual(self.transport.call_count, 0, "Must not make network calls on invalid order_type")

        # Valid order types accepted
        self.assertEqual(rails._hyperliquid_order_type_to_wire("limit"), {"limit": {"tif": "Gtc"}})
        self.assertEqual(
            rails._hyperliquid_order_type_to_wire({"limit": {"tif": "Gtc"}}),
            {"limit": {"tif": "Gtc"}},
        )
        self.assertEqual(
            rails._hyperliquid_order_type_to_wire({"limit": {"tif": "Ioc"}}),
            {"limit": {"tif": "Ioc"}},
        )
        self.assertEqual(
            rails._hyperliquid_order_type_to_wire({"limit": {"tif": "Alo"}}),
            {"limit": {"tif": "Alo"}},
        )

        # Malformed order types rejected
        bad_types = [
            {"limit": {"tif": "Fok"}},
            {"limit": {"tif": "Gtc", "extra": 1}},
            {"limit": "Gtc"},
            {"trigger": {"isMarket": "not_a_bool", "triggerPx": 90000, "tpsl": "sl"}},
            {"trigger": {"isMarket": True, "triggerPx": 90000, "tpsl": "invalid"}},
            {"trigger": {"isMarket": True, "triggerPx": 90000}},
            {"trigger": {"isMarket": True, "triggerPx": 90000, "tpsl": "sl", "extra": 1}},
            {"unknown": 123},
            "stop_loss",
            123,
            None,
        ]
        for bad in bad_types:
            with self.assertRaises(ValueError):
                rails.build_hyperliquid_order_payload(
                    coin="BTC",
                    is_buy=True,
                    sz=0.001,
                    px=97000,
                    order_type=bad,
                    config=self.config,
                    transport=self.transport,
                )
            self.assertEqual(self.transport.call_count, 0)

    def test_gap3_nan_inf_negative_bool_rejected_zero_network(self):
        """Gap 3: NaN, inf, -inf, <= 0, and bool raise ValueError before network calls."""
        # Non-finite / non-positive px
        for bad_px in (float("nan"), float("inf"), float("-inf"), 0, -100, True, False):
            with self.assertRaises(ValueError):
                rails.build_hyperliquid_order_payload(
                    coin="BTC",
                    is_buy=True,
                    sz=0.001,
                    px=bad_px,
                    config=self.config,
                    transport=self.transport,
                )
            self.assertEqual(self.transport.call_count, 0)

        # Non-finite / non-positive sz
        for bad_sz in (float("nan"), float("inf"), float("-inf"), 0, -1, True, False):
            with self.assertRaises(ValueError):
                rails.build_hyperliquid_order_payload(
                    coin="BTC",
                    is_buy=True,
                    sz=bad_sz,
                    px=97000,
                    config=self.config,
                    transport=self.transport,
                )
            self.assertEqual(self.transport.call_count, 0)

        # Non-finite / non-positive triggerPx
        for bad_tpx in (float("nan"), float("inf"), float("-inf"), 0, -50, True, False):
            with self.assertRaises(ValueError):
                rails.build_hyperliquid_order_payload(
                    coin="BTC",
                    is_buy=False,
                    sz=0.001,
                    px=90000,
                    order_type={"trigger": {"isMarket": True, "triggerPx": bad_tpx, "tpsl": "sl"}},
                    config=self.config,
                    transport=self.transport,
                )
            self.assertEqual(self.transport.call_count, 0)

    def test_gap4_spot_tokens_shuffled_index_resolution(self):
        """Gap 4: spotMeta.tokens permutation is resolved by token['index'], matching SDK."""
        shuffled = copy.deepcopy(FIXTURE_SPOT_META)
        # Permute tokens out of order: [USDC(0), HYPE(3), PURR(1), HFUN(2)]
        shuffled["tokens"] = [shuffled["tokens"][i] for i in (0, 3, 1, 2)]
        shuffled_transport = MockTransport(spot_meta=shuffled)

        payload = rails.build_hyperliquid_order_payload(
            coin="HYPE",
            is_buy=True,
            sz=1.5,
            px=38.5,
            is_spot=True,
            config=self.config,
            transport=shuffled_transport,
        )
        # 10000 + 107 = 10107
        self.assertEqual(payload["action"]["orders"][0]["a"], 10107)
        self.assertEqual(payload["coin"], "@107")

    def test_polymarket_hedge_bundle_custom_transport(self):
        """Verify build_polymarket_hyperliquid_hedge_bundle forwards transport."""
        bundle = rails.build_polymarket_hyperliquid_hedge_bundle(
            polymarket_token_id="12345",
            polymarket_price="0.55",
            polymarket_size="100",
            polymarket_side="BUY",
            hyperliquid_coin="BTC",
            hyperliquid_is_buy=False,
            hyperliquid_sz="0.01",
            hyperliquid_px="60000",
            config=self.config,
            transport=self.transport,
        )
        self.assertEqual(bundle["bundle_type"], "polymarket_hyperliquid_cross_hedge")
        self.assertEqual(bundle["legs"]["hyperliquid_perp_hedge_leg"]["action"]["orders"][0]["a"], 0)
        self.assertEqual(self.transport.call_count, 1)


# ============================================================================
# Test Suite 2: Full hyperliquid-python-sdk 0.24 Parity Battery
# ============================================================================
@unittest.skipUnless(HAS_HL_SDK, "hyperliquid-python-sdk not installed in this interpreter")
class HyperliquidParitySDKTests(unittest.TestCase):
    """Port of Claude's 28+ check_fix cases with msgpack, EIP-712 and action_hash parity."""

    def setUp(self):
        if not HAS_HL_SDK:
            return
        hl_api.API.post = self._no_network
        self.transport = MockTransport()
        self.info = Info(
            base_url="https://api.hyperliquid.xyz",
            skip_ws=True,
            meta=FIXTURE_META,
            spot_meta=FIXTURE_SPOT_META,
        )
        self.wallet = Account.create()
        self.config = _make_config(30)

    @staticmethod
    def _no_network(*_args, **_kwargs):
        raise AssertionError("The SDK tried to reach the external network during tests")

    def _sdk_order_action(self, name: str, kwargs: Dict[str, Any], order_type: Any, tenths: Any):
        order = {
            "coin": name,
            "is_buy": kwargs["is_buy"],
            "sz": float(kwargs["sz"]),
            "limit_px": float(kwargs["px"]),
            "order_type": order_type,
            "reduce_only": kwargs.get("reduce_only", False),
        }
        if kwargs.get("cloid"):
            order["cloid"] = Cloid.from_str(kwargs["cloid"])
        wire = signing.order_request_to_order_wire(order, self.info.name_to_asset(name))
        builder = None
        if tenths is not None:
            builder = {"b": TEST_BUILDER.lower(), "f": tenths}
        return signing.order_wires_to_order_action([wire], builder)

    def _assert_action_parity(self, ours: Dict[str, Any], sdk: Dict[str, Any]):
        """Asserts 100% byte and signature parity between ours and the SDK."""
        # 1. Dict equality
        self.assertEqual(ours, sdk, "Action dictionaries differ")
        # 2. JSON key serialization order
        self.assertEqual(json.dumps(ours), json.dumps(sdk), "Key ordering differs")
        # 3. Canonical msgpack binary encoding
        self.assertEqual(msgpack.packb(ours), msgpack.packb(sdk), "msgpack encoding differs")
        # 4. Action hash
        self.assertEqual(
            signing.action_hash(ours, None, TEST_NONCE, None),
            signing.action_hash(sdk, None, TEST_NONCE, None),
            "Action hash differs",
        )
        # 5. EIP-712 signature parity
        sig_ours = signing.sign_l1_action(self.wallet, ours, None, TEST_NONCE, None, True)
        sig_sdk = signing.sign_l1_action(self.wallet, sdk, None, TEST_NONCE, None, True)
        self.assertEqual(sig_ours, sig_sdk, "EIP-712 signatures differ")
        # 6. User address recovery
        recovered = signing.recover_agent_or_user_from_l1_action(
            ours, sig_ours, None, TEST_NONCE, None, True
        )
        self.assertEqual(recovered, self.wallet.address, "Recovered address differs from signer")

    def test_cases_1_to_15_all_orders_parity(self):
        """Port of all 15 core order variations from check_fix.py."""
        gtc = {"limit": {"tif": "Gtc"}}
        ioc = {"limit": {"tif": "Ioc"}}
        alo = {"limit": {"tif": "Alo"}}
        trigger_wire = {"trigger": {"isMarket": True, "triggerPx": "90000", "tpsl": "sl"}}
        trigger_sdk = {"trigger": {"isMarket": True, "triggerPx": 90000.0, "tpsl": "sl"}}

        cases = [
            ("perp BTC Gtc, floats", dict(coin="BTC", is_buy=True, sz=0.001, px=97000.0), "BTC", gtc, 30),
            (
                "perp ETH Ioc, strings, reduce-only",
                dict(coin="ETH", is_buy=False, sz="0.5", px="3500.5", order_type=ioc, reduce_only=True),
                "ETH",
                ioc,
                30,
            ),
            (
                "perp SOL despues de un deslistado, Alo",
                dict(coin="SOL", is_buy=True, sz=1.23, px=150.25, order_type=alo),
                "SOL",
                alo,
                30,
            ),
            ("perp kPEPE px 0.00001", dict(coin="kPEPE", is_buy=True, sz=1000, px=0.00001), "kPEPE", gtc, 30),
            ("perp 'kpepe' en minusculas", dict(coin="kpepe", is_buy=False, sz=2000, px=0.000012), "kPEPE", gtc, 30),
            ("perp DOGE con cloid", dict(coin="DOGE", is_buy=True, sz=100, px=0.16, cloid=TEST_CLOID), "DOGE", gtc, 30),
            ("perp BTC sin builder", dict(coin="BTC", is_buy=True, sz=0.01, px=97000), "BTC", gtc, None),
            ("perp BTC fee 100 (tope)", dict(coin="BTC", is_buy=True, sz=0.001, px=97000), "BTC", gtc, 100),
            ("perp BTC sz 0.1+0.2", dict(coin="BTC", is_buy=True, sz=0.1 + 0.2, px=97001), "BTC", gtc, 30),
            ("perp DOGE entero grande", dict(coin="DOGE", is_buy=True, sz=12345678, px=1), "DOGE", gtc, 30),
            (
                "perp BTC trigger ya en formato wire",
                dict(coin="BTC", is_buy=False, sz=0.001, px=90000, order_type=trigger_wire, reduce_only=True),
                "BTC",
                trigger_sdk,
                30,
            ),
            ("spot PURR/USDC", dict(coin="PURR/USDC", is_buy=True, sz=10, px=0.2, is_spot=True), "PURR/USDC", gtc, 30),
            ("spot 'HYPE' pelado", dict(coin="HYPE", is_buy=True, sz=1.5, px=38.5, is_spot=True), "HYPE/USDC", gtc, 30),
            ("spot '@1'", dict(coin="@1", is_buy=False, sz=3, px=7.25, is_spot=True), "@1", gtc, 30),
            (
                "spot fee 1000 (tope)",
                dict(coin="PURR/USDC", is_buy=True, sz=10, px=0.2, is_spot=True),
                "PURR/USDC",
                gtc,
                1000,
            ),
        ]

        for label, kwargs, sdk_name, sdk_type, tenths in cases:
            with self.subTest(case=label):
                cfg = _make_config(30 if tenths is None else tenths, active=tenths is not None)
                payload = rails.build_hyperliquid_order_payload(
                    config=cfg,
                    transport=self.transport,
                    **kwargs,
                )
                sdk_action = self._sdk_order_action(sdk_name, kwargs, sdk_type, tenths)
                self._assert_action_parity(payload["action"], sdk_action)

    def test_cases_16_to_22_rejection_parity(self):
        """Port of all 7 negative validation cases from check_fix.py."""
        negative_cases = [
            ("perp fee 101 > tope", dict(coin="BTC", is_buy=True, sz=0.001, px=97000), _make_config(101), 0),
            ("spot fee 1001 > tope", dict(coin="PURR/USDC", is_buy=True, sz=10, px=0.2, is_spot=True), _make_config(1001), 0),
            ("perp deslistado OLD", dict(coin="OLD", is_buy=True, sz=1, px=1), _make_config(30), 1),
            ("perp desconocido", dict(coin="NOPE", is_buy=True, sz=1, px=1), _make_config(30), 1),
            ("sz con 9 decimales", dict(coin="BTC", is_buy=True, sz=0.123456789, px=97000), _make_config(30), 0),
            ("px 0", dict(coin="BTC", is_buy=True, sz=0.001, px=0), _make_config(30), 0),
            ("sz negativo", dict(coin="BTC", is_buy=True, sz=-1, px=97000), _make_config(30), 0),
        ]

        for label, kwargs, cfg, want_calls in negative_cases:
            with self.subTest(case=label):
                transport = MockTransport()
                with self.assertRaises(ValueError):
                    rails.build_hyperliquid_order_payload(config=cfg, transport=transport, **kwargs)
                self.assertEqual(
                    transport.call_count,
                    want_calls,
                    f"{label} made unexpected number of /info calls",
                )

    def test_cases_23_to_28_approve_builder_fee_tiers(self):
        """Port of all 6 approveBuilderFee tiers (1, 5, 10, 30, 100, 1000 spot) from check_fix.py."""
        captured = []
        original_sign_inner = signing.sign_inner

        def capture_sign_inner(wallet, data):
            captured.append(copy.deepcopy(data))
            return original_sign_inner(wallet, data)

        signing.sign_inner = capture_sign_inner
        try:
            expected_rates = {1: "0.001%", 5: "0.005%", 10: "0.01%", 30: "0.03%", 100: "0.1%", 1000: "1%"}
            for tenths, spot in ((1, False), (5, False), (10, False), (30, False), (100, False), (1000, True)):
                with self.subTest(tenths=tenths, spot=spot):
                    if spot:
                        kwargs = dict(coin="PURR/USDC", is_buy=True, sz=10, px=0.2, is_spot=True)
                    else:
                        kwargs = dict(coin="BTC", is_buy=True, sz=0.001, px=97000)
                    t0 = int(time.time() * 1000)
                    payload = rails.build_hyperliquid_order_payload(
                        config=_make_config(tenths),
                        transport=self.transport,
                        **kwargs,
                    )
                    t1 = int(time.time() * 1000)
                    ours = payload["prerequisite_approve_builder_fee"]

                    self.assertEqual(ours["maxFeeRate"], expected_rates[tenths])
                    self.assertEqual(
                        Decimal(ours["maxFeeRate"].rstrip("%")) * 1000,
                        payload["action"]["builder"]["f"],
                    )
                    self.assertTrue(t0 <= ours["nonce"] <= t1)

                    sdk_action = {
                        "maxFeeRate": ours["maxFeeRate"],
                        "builder": TEST_BUILDER,
                        "nonce": ours["nonce"],
                        "type": "approveBuilderFee",
                    }
                    captured.clear()
                    sdk_sig = signing.sign_approve_builder_fee(self.wallet, sdk_action, True)
                    typed_sdk = captured[0]

                    # Verify exact match of payload fields
                    primary = typed_sdk["primaryType"]
                    fields = typed_sdk["types"][primary]
                    typed_ours = signing.user_signed_payload(primary, fields, ours)
                    self.assertEqual(
                        encode_typed_data(full_message=typed_ours),
                        encode_typed_data(full_message=typed_sdk),
                    )

                    our_sig = original_sign_inner(self.wallet, typed_ours)
                    self.assertEqual(our_sig, sdk_sig)

                    recovered = signing.recover_user_from_user_signed_action(
                        copy.deepcopy(ours), our_sig, fields, primary, True
                    )
                    self.assertEqual(recovered, self.wallet.address)
        finally:
            signing.sign_inner = original_sign_inner

    def test_sdk_trigger_float_full_signing_parity(self):
        """Explicit parity test for TRIGGER_SDK with float triggerPx (closing Gap 1 with SDK)."""
        trigger_sdk = {"trigger": {"isMarket": True, "triggerPx": 90000.0, "tpsl": "sl"}}
        kwargs = dict(coin="BTC", is_buy=False, sz=0.001, px=90000, order_type=copy.deepcopy(trigger_sdk), reduce_only=True)
        payload = rails.build_hyperliquid_order_payload(
            config=_make_config(30),
            transport=self.transport,
            **kwargs,
        )
        sdk_action = self._sdk_order_action("BTC", kwargs, trigger_sdk, 30)
        self._assert_action_parity(payload["action"], sdk_action)

    def test_sdk_order_type_market_raises_value_error(self):
        """Verifies that hyperliquid.utils.signing.order_type_to_wire('market') raises ValueError, matching our rail."""
        with self.assertRaises(ValueError):
            signing.order_type_to_wire("market")

        with self.assertRaises(ValueError):
            rails.build_hyperliquid_order_payload(
                coin="BTC",
                is_buy=True,
                sz=0.001,
                px=97000,
                order_type="market",
                config=self.config,
                transport=self.transport,
            )


if __name__ == "__main__":
    unittest.main()
