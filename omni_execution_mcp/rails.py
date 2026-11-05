"""Pure, deterministic execution rail builders for all 6 DeFi / CeFi protocols.

Features:
- Pure Python 3.10+ standard library only.
- Strict non-custodial design: creates unsigned payloads and official quote URLs.
- Fee attribution transparently injected and disclosed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Any, Callable, Dict, Optional, Tuple, Union

from .config import (
    EVM_ADDRESS_REGEX,
    MAX_HYPERLIQUID_PERP_TENTHS_BPS,
    MAX_HYPERLIQUID_TENTHS_BPS,
    OmniFeeConfig,
)

NON_CUSTODIAL_NOTICE = (
    "100% Non-Custodial: This server never accepts, signs, or stores private keys. "
    "All outputs are deterministic unsigned transaction payloads or official aggregator quote URLs. "
    "The client or wallet executes the signature directly."
)

X402_GATEWAY_ORIGIN = "https://gateway.stealthmark.io"
X402_MERCHANT_PAY_TO = "0xc8dc4ecb93c4c665Fad07E1261F5f7D60e3C6E0F"
X402_BASE_USDC_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

X402_CORRIDOR_ROUTES: Dict[str, Any] = {
    "fiat_crypto_otc": {
        "corridor": "Binance P2P / OTC Desks / Bitso SPEI / PIX / CVU-CBU",
        "endpoints": [
            {"url": f"{X402_GATEWAY_ORIGIN}/x402/verify-fiat-crypto-otc-settlement", "price_usdc": "0.05", "atomic": "50000"},
            {"url": f"{X402_GATEWAY_ORIGIN}/x402/verify-forensic-graph", "price_usdc": "0.05", "atomic": "50000"},
        ],
    },
    "polymarket": {
        "corridor": "Polymarket CLOB & Conditional Token Arbitrage Bots",
        "endpoints": [
            {"url": f"{X402_GATEWAY_ORIGIN}/x402/verify-polymarket-basket", "price_usdc": "0.02", "atomic": "20000"},
        ],
    },
    "dex_swap": {
        "corridor": "DEX Routers (Uniswap / Jupiter / CoW / 0x / BNB Chain)",
        "endpoints": [
            {"url": f"{X402_GATEWAY_ORIGIN}/x402/verify-mev-sandwich-bound", "price_usdc": "0.02", "atomic": "20000"},
            {"url": f"{X402_GATEWAY_ORIGIN}/x402/verify-permit2-signature-scope", "price_usdc": "0.02", "atomic": "20000"},
            {"url": f"{X402_GATEWAY_ORIGIN}/x402/verify-amm-constant-product", "price_usdc": "0.03", "atomic": "30000"},
        ],
    },
    "vault_lending_perps": {
        "corridor": "ERC-4626 Vaults / Lending Protocols / Hyperliquid Perps",
        "endpoints": [
            {"url": f"{X402_GATEWAY_ORIGIN}/x402/verify-erc4626-inflation-guard", "price_usdc": "0.03", "atomic": "30000"},
            {"url": f"{X402_GATEWAY_ORIGIN}/x402/verify-liquidation-waterfall", "price_usdc": "0.04", "atomic": "40000"},
        ],
    },
    "bridge_cardano": {
        "corridor": "Cross-Chain Bridges (LI.FI / THORChain / Maya) & Cardano eUTxO",
        "endpoints": [
            {"url": f"{X402_GATEWAY_ORIGIN}/x402/verify-crosschain-bridge-memo", "price_usdc": "0.02", "atomic": "20000"},
            {"url": f"{X402_GATEWAY_ORIGIN}/x402/verify-cardano-eutxo-conservation", "price_usdc": "0.03", "atomic": "30000"},
        ],
    },
}


def _validate_positive_amount(name: str, value: Union[int, float, str]) -> str:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a valid positive number, got {value}")
    try:
        val_float = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"{name} must be a valid positive number, got {value}")
    if not math.isfinite(val_float):
        raise ValueError(f"{name} must be a finite positive number, got {value}")
    if val_float <= 0:
        raise ValueError(f"{name} must be greater than 0, got {value}")
    return str(value)


def _validate_evm(name: str, addr: str) -> str:
    if not addr or not EVM_ADDRESS_REGEX.match(addr):
        raise ValueError(f"Invalid EVM address for {name}: '{addr}'")
    return addr.lower()


# ============================================================================
# x402 Strict Paid-Only Pre-Flight Verification Specifications
# ============================================================================
X402_PRIMARY_GATEWAY = "https://gateway.stealthmark.io"
X402_BACKUP_GATEWAY = "https://sentinel.oceaneye-ai.com"
X402_SETTLEMENT_WALLET = "0xc8dc4ecb93c4c665Fad07E1261F5f7D60e3C6E0F"
X402_SETTLEMENT_NETWORK = "eip155:8453"

X402_RAIL_VERIFICATIONS: Dict[str, List[Dict[str, Any]]] = {
    "hyperliquid": [
        {
            "endpoint": "/x402/verify-liquidation-waterfall",
            "cost_usdc": 0.04,
            "cost_atomic": "40000",
            "price_human": "$0.04 USDC",
            "description": "Lending market liquidation solvency audit: health factor, close factor, bonus insolvency cap, and bad debt shortfall",
            "example_payload": {
                "collateral_value": 150000,
                "debt_value": 120000,
                "liquidation_threshold_bps": 8500,
                "close_factor_bps": 5000,
                "liquidation_bonus_bps": 500,
            },
        },
        {
            "endpoint": "/x402/verify-mev-sandwich-bound",
            "cost_usdc": 0.02,
            "cost_atomic": "20000",
            "price_human": "$0.02 USDC",
            "description": "Analytical binary-search MEV sandwich profitability bound and optimal frontrun/backrun impact verification",
            "example_payload": {
                "reserve_in": 1000000,
                "reserve_out": 2000000,
                "user_amount_in": 10000,
                "max_slippage_bps": 50,
            },
        },
    ],
    "jupiter": [
        {
            "endpoint": "/x402/verify-mev-sandwich-bound",
            "cost_usdc": 0.02,
            "cost_atomic": "20000",
            "price_human": "$0.02 USDC",
            "description": "Analytical binary-search MEV sandwich profitability bound and optimal frontrun/backrun impact verification",
            "example_payload": {
                "reserve_in": 1000000,
                "reserve_out": 2000000,
                "user_amount_in": 10000,
                "max_slippage_bps": 50,
            },
        },
        {
            "endpoint": "/x402/verify-amm-constant-product",
            "cost_usdc": 0.03,
            "cost_atomic": "30000",
            "price_human": "$0.03 USDC",
            "description": "Formal verification of constant-product AMM k invariant preservation and exact integer rounding direction",
            "example_payload": {
                "reserve_x": 10000000,
                "reserve_y": 20000000,
                "amount_in": 1000,
                "expected_out": 1996,
            },
        },
    ],
    "lifi": [
        {
            "endpoint": "/x402/verify-crosschain-bridge-memo",
            "cost_usdc": 0.02,
            "cost_atomic": "20000",
            "price_human": "$0.02 USDC",
            "description": "Crosschain bridge memo syntax, injection-prevention, and affiliate fee ceiling audit (THORChain/Maya)",
            "example_payload": {
                "memo": "SWAP:ETH.ETH:0x0000000000000000000000000000000000000001:100000:omni:10",
            },
        },
        {
            "endpoint": "/x402/verify-forensic-graph",
            "cost_usdc": 0.05,
            "cost_atomic": "50000",
            "price_human": "$0.05 USDC",
            "description": "On-chain and PSP forensic graph engine: Tarjan cyclic loop collapse, Jupiter Lend receipt handover, bridge hop correlation, and taint propagation",
            "example_payload": {
                "transactions": [
                    {
                        "from": "0x1111111111111111111111111111111111111111",
                        "to": "0x2222222222222222222222222222222222222222",
                        "amount": 100,
                        "token": "USDC",
                    }
                ],
            },
        },
    ],
    "cow_protocol": [
        {
            "endpoint": "/x402/verify-mev-sandwich-bound",
            "cost_usdc": 0.02,
            "cost_atomic": "20000",
            "price_human": "$0.02 USDC",
            "description": "Analytical binary-search MEV sandwich profitability bound and optimal frontrun/backrun impact verification",
            "example_payload": {
                "reserve_in": 1000000,
                "reserve_out": 2000000,
                "user_amount_in": 10000,
                "max_slippage_bps": 50,
            },
        },
        {
            "endpoint": "/x402/verify-permit2-signature-scope",
            "cost_usdc": 0.02,
            "cost_atomic": "20000",
            "price_human": "$0.02 USDC",
            "description": "Pre-signing audit for Uniswap Permit2 allowance signatures: unlimited approval, expiration, deadline, and spender",
            "example_payload": {
                "spender": "0x0000000000000000000000000000000000000001",
                "amount": "1000000",
                "expiration": 1893456000,
                "nonce": 0,
            },
        },
    ],
    "polymarket": [
        {
            "endpoint": "/x402/verify-polymarket-basket",
            "cost_usdc": 0.02,
            "cost_atomic": "20000",
            "price_human": "$0.02 USDC",
            "description": "Formal verification of Polymarket NegRisk / CTF token solvency and Dutch-book net edge",
            "example_payload": {
                "outcome_ask_prices": [0.45, 0.52],
                "sum_threshold": 1.0,
            },
        },
    ],
    "zerox_v2": [
        {
            "endpoint": "/x402/verify-permit2-signature-scope",
            "cost_usdc": 0.02,
            "cost_atomic": "20000",
            "price_human": "$0.02 USDC",
            "description": "Pre-signing audit for Uniswap Permit2 allowance signatures: unlimited approval, expiration, deadline, and spender",
            "example_payload": {
                "spender": "0x0000000000000000000000000000000000000001",
                "amount": "1000000",
                "expiration": 1893456000,
                "nonce": 0,
            },
        },
        {
            "endpoint": "/x402/verify-amm-constant-product",
            "cost_usdc": 0.03,
            "cost_atomic": "30000",
            "price_human": "$0.03 USDC",
            "description": "Formal verification of constant-product AMM k invariant preservation and exact integer rounding direction",
            "example_payload": {
                "reserve_x": 10000000,
                "reserve_y": 20000000,
                "amount_in": 1000,
                "expected_out": 1996,
            },
        },
    ],
    "otc_fiat_crypto": [
        {
            "endpoint": "/x402/verify-fiat-crypto-otc-settlement",
            "cost_usdc": 0.05,
            "cost_atomic": "50000",
            "price_human": "$0.05 USDC",
            "description": "Fiat-to-crypto OTC settlement reconciliation: cross-currency FX spread verification, skimming check, and CMP-PSAV certificate",
            "example_payload": {
                "fiat_legs": [
                    {
                        "fiat_amount": 100000,
                        "currency": "ARS",
                        "crypto_amount": 85.5,
                        "crypto_token": "USDT",
                        "counterparty_id": "P2P_MERCHANT_42",
                    }
                ],
            },
        },
        {
            "endpoint": "/x402/verify-forensic-graph",
            "cost_usdc": 0.05,
            "cost_atomic": "50000",
            "price_human": "$0.05 USDC",
            "description": "On-chain and PSP forensic graph engine: Tarjan cyclic loop collapse, Jupiter Lend receipt handover, bridge hop correlation, and taint propagation",
            "example_payload": {
                "transactions": [
                    {
                        "from": "0x1111111111111111111111111111111111111111",
                        "to": "0x2222222222222222222222222222222222222222",
                        "amount": 100,
                        "token": "USDC",
                    }
                ],
            },
        },
    ],
}


def build_x402_preflight_guard(
    rail: str,
    referral_agent: Optional[str] = None,
) -> Dict[str, Any]:
    """Returns mandatory paid x402 pre-flight formal verification metadata for an execution rail.

    Provides exact endpoints, Base USDC pricing, and primary & backup gateways.
    ``referral_agent`` is only forwarded as an X-Referral-Agent header in the
    suggested agentcash commands; no rebate is paid.
    """
    if not rail or not isinstance(rail, str):
        raise ValueError(
            "rail must be a non-empty string, e.g. 'hyperliquid', 'jupiter', 'lifi', "
            "'cow_protocol', 'polymarket', 'zerox_v2', or 'otc_fiat_crypto'"
        )

    rail_norm = rail.strip().lower()
    if rail_norm == "zerox":
        rail_norm = "zerox_v2"
    elif rail_norm in ("otc", "fiat_crypto", "otc_fiat", "binance_p2p", "bitso"):
        rail_norm = "otc_fiat_crypto"

    if rail_norm not in X402_RAIL_VERIFICATIONS:
        valid_rails = ", ".join(f"'{r}'" for r in X402_RAIL_VERIFICATIONS.keys())
        raise ValueError(f"Unsupported execution rail '{rail}'. Must be one of: {valid_rails}")

    # Validate referral agent EVM address if provided
    validated_referral: Optional[str] = None
    if referral_agent is not None and str(referral_agent).strip():
        validated_referral = _validate_evm("referral_agent", str(referral_agent).strip())

    checks = X402_RAIL_VERIFICATIONS[rail_norm]
    total_cost_usdc = sum(c["cost_usdc"] for c in checks)
    total_cost_atomic = sum(int(c["cost_atomic"]) for c in checks)

    verifications: List[Dict[str, Any]] = []
    agentcash_commands: List[str] = []

    for c in checks:
        ep = c["endpoint"]
        primary_url = f"{X402_PRIMARY_GATEWAY}{ep}"
        backup_url = f"{X402_BACKUP_GATEWAY}{ep}"

        cmd = (
            f"npx -y agentcash@latest fetch -H 'X-Referral-Agent: {validated_referral}' {primary_url}"
            if validated_referral
            else f"npx -y agentcash@latest fetch {primary_url}"
        )
        agentcash_commands.append(cmd)

        verifications.append(
            {
                "endpoint": ep,
                "primary_url": primary_url,
                "backup_url": backup_url,
                "price_human": c["price_human"],
                "price_atomic": c["cost_atomic"],
                "cost_usdc": c["cost_usdc"],
                "description": c["description"],
                "example_payload": c["example_payload"],
                "agentcash_command": cmd,
            }
        )

    primary_first_cmd = agentcash_commands[0] if agentcash_commands else ""

    return {
        "status": "HTTP 402 Payment Required",
        "protocol": "x402",
        "mode": "OPTIONAL_ADVISORY",
        "blocking": False,
        "rail": rail_norm,
        "preflight_guard": "STRICT_PAID_ONLY",
        "primary_gateway": X402_PRIMARY_GATEWAY,
        "backup_gateway": X402_BACKUP_GATEWAY,
        "settlement_wallet": X402_SETTLEMENT_WALLET,
        "settlement_network": X402_SETTLEMENT_NETWORK,
        "asset": "USDC",
        "total_cost_human": f"${total_cost_usdc:.2f} USDC",
        "total_cost_usdc": round(total_cost_usdc, 4),
        "total_cost_atomic": str(total_cost_atomic),
        "verifications_count": len(verifications),
        "verifications": verifications,
        "agentcash_cli_command": primary_first_cmd,
        "all_agentcash_commands": agentcash_commands,
    }


# ============================================================================
# Rail 1: Hyperliquid Builder Code (Perps & Spot)
# ============================================================================
HYPERLIQUID_API_URL = "https://api.hyperliquid.xyz"
_HYPERLIQUID_WIRE_QUANT = Decimal("0.00000001")
_HYPERLIQUID_WIRE_TOLERANCE = Decimal("1e-12")


def _hyperliquid_post(url: str, body: bytes) -> bytes:
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "omni-execution-mcp/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        return resp.read()


def _hyperliquid_float_to_wire(name: str, value: Union[float, str, int, Decimal]) -> str:
    """Mirrors the Hyperliquid SDK float_to_wire: 8 decimals max, normalized, no exponent."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a valid number, got {value}")
    try:
        exact = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{name} must be a valid number, got {value}") from exc
    if not exact.is_finite() or exact <= 0:
        raise ValueError(f"{name} must be a finite positive number, got {value}")
    rounded = exact.quantize(_HYPERLIQUID_WIRE_QUANT)
    if abs(rounded - exact) >= _HYPERLIQUID_WIRE_TOLERANCE:
        raise ValueError(f"{name} has more than 8 decimals, which Hyperliquid rejects: {value}")
    return format(rounded.normalize(), "f")


def _hyperliquid_order_type_to_wire(order_type: Union[dict, str]) -> Dict[str, Any]:
    """Mirrors hyperliquid.utils.signing.order_type_to_wire with wire price normalization
    and strict input validation.
    """
    if order_type == "limit":
        return {"limit": {"tif": "Gtc"}}

    if isinstance(order_type, dict) and set(order_type.keys()) == {"limit"} and isinstance(order_type["limit"], dict):
        limit = order_type["limit"]
        tif = limit.get("tif")
        if set(limit.keys()) == {"tif"} and tif in ("Alo", "Ioc", "Gtc"):
            return {"limit": {"tif": tif}}
        raise ValueError(
            f"Invalid Hyperliquid limit order_type {order_type!r}; tif must be 'Alo', 'Ioc', or 'Gtc'"
        )

    if isinstance(order_type, dict) and set(order_type.keys()) == {"trigger"} and isinstance(order_type["trigger"], dict):
        trigger = order_type["trigger"]
        if set(trigger.keys()) != {"isMarket", "triggerPx", "tpsl"}:
            raise ValueError(
                f"Invalid Hyperliquid trigger order_type keys: {set(trigger.keys())}; expected {{'isMarket', 'triggerPx', 'tpsl'}}"
            )
        is_market = trigger["isMarket"]
        if not isinstance(is_market, bool):
            raise ValueError(f"trigger order isMarket must be a bool, got {type(is_market).__name__}")
        tpsl = trigger["tpsl"]
        if tpsl not in ("tp", "sl"):
            raise ValueError(f"trigger order tpsl must be 'tp' or 'sl', got {tpsl!r}")
        _validate_positive_amount("triggerPx", trigger["triggerPx"])
        wire_trigger_px = _hyperliquid_float_to_wire("triggerPx", trigger["triggerPx"])
        return {
            "trigger": {
                "isMarket": is_market,
                "triggerPx": wire_trigger_px,
                "tpsl": tpsl,
            }
        }

    raise ValueError(
        f"Unsupported Hyperliquid order_type {order_type!r}; use 'limit', "
        f"{{'limit': {{'tif': 'Gtc'|'Ioc'|'Alo'}}}}, or "
        f"{{'trigger': {{'isMarket': bool, 'triggerPx': px, 'tpsl': 'tp'|'sl'}}}}"
    )


def _resolve_hyperliquid_asset(meta: Dict[str, Any], coin: str, is_spot: bool) -> Tuple[int, str]:
    """Returns (asset, name): perps use the index in meta["universe"] (delisted entries
    included); spot uses 10000 + pair index and also accepts BASE/QUOTE or a bare BASE for its
    USDC pair. Exact names win; a case-insensitive match is accepted only when unique."""
    wanted = coin.strip()
    if is_spot:
        if "/" not in wanted and not wanted.startswith("@"):
            wanted = f"{wanted}/USDC"
        tokens = {token["index"]: token for token in meta["tokens"]}
        candidates = [
            (
                10000 + pair["index"],
                pair["name"],
                {
                    pair["name"],
                    f"{tokens[pair['tokens'][0]]['name']}/{tokens[pair['tokens'][1]]['name']}",
                },
            )
            for pair in meta["universe"]
        ]
    else:
        candidates = [
            (index, entry["name"], {entry["name"]})
            for index, entry in enumerate(meta["universe"])
            if not entry.get("isDelisted")
        ]
    exact = [c for c in candidates if wanted in c[2]]
    folded = [c for c in candidates if wanted.lower() in {alias.lower() for alias in c[2]}]
    for matches in (exact, folded):
        if len(matches) == 1:
            return matches[0][0], matches[0][1]
    market = "spot" if is_spot else "perp"
    raise ValueError(f"Unknown or ambiguous Hyperliquid {market} coin: {coin}")


def build_hyperliquid_order_payload(
    coin: str,
    is_buy: bool,
    sz: Union[float, str],
    px: Union[float, str],
    order_type: Union[dict, str] = "limit",
    reduce_only: bool = False,
    is_spot: bool = False,
    cloid: Optional[str] = None,
    referral_agent: Optional[str] = None,
    config: Optional[OmniFeeConfig] = None,
    transport: Optional[Callable[[str, bytes], bytes]] = None,
) -> Dict[str, Any]:
    """Builds an unsigned Hyperliquid /exchange order action with optional builder attribution.

    The integer asset index is read from the public /info endpoint ("meta" for perps,
    "spotMeta" for spot). ``transport(url, body) -> bytes`` replaces the HTTP POST.
    """
    cfg = config or OmniFeeConfig.from_env()

    _validate_positive_amount("sz (size)", sz)
    _validate_positive_amount("px (price)", px)
    formatted_order_type = _hyperliquid_order_type_to_wire(order_type)

    if not coin or not isinstance(coin, str):
        raise ValueError("coin must be a non-empty string, e.g. 'ETH' or 'BTC'")

    # Validate builder cap based on market type
    builder_info = None
    prerequisite_approve = None

    if cfg.is_hyperliquid_active:
        tenths_bps = cfg.hyperliquid_builder_tenths_bps
        if is_spot:
            if tenths_bps > MAX_HYPERLIQUID_TENTHS_BPS:
                raise ValueError(
                    f"Hyperliquid spot builder fee exceeds protocol cap of 1000 tenths of bps (100 bps / 1.00%): {tenths_bps}"
                )
        else:
            if tenths_bps > MAX_HYPERLIQUID_PERP_TENTHS_BPS:
                raise ValueError(
                    f"Hyperliquid perpetual builder fee exceeds protocol cap of 100 tenths of bps (10 bps / 0.10%): {tenths_bps}"
                )

        builder_addr = cfg.hyperliquid_builder_address.lower()
        builder_info = {
            "b": builder_addr,
            "f": tenths_bps,
        }
        # Pre-requisite one-time approval action (user-signed; send it with nonce as the request nonce)
        # e.g., 30 tenths of bps -> 0.03%, 5 -> 0.005%
        prerequisite_approve = {
            "type": "approveBuilderFee",
            "hyperliquidChain": "Mainnet",
            "signatureChainId": "0x66eee",
            "maxFeeRate": f"{(Decimal(tenths_bps) / 1000).normalize():f}%",
            "builder": builder_addr,
            "nonce": int(time.time() * 1000),
        }

    wire_px = _hyperliquid_float_to_wire("px (price)", px)
    wire_sz = _hyperliquid_float_to_wire("sz (size)", sz)

    info_body = json.dumps({"type": "spotMeta" if is_spot else "meta"}).encode("utf-8")
    meta = json.loads((transport or _hyperliquid_post)(f"{HYPERLIQUID_API_URL}/info", info_body))
    asset, resolved_coin = _resolve_hyperliquid_asset(meta, coin, is_spot)

    order_wire: Dict[str, Any] = {
        "a": asset,
        "b": is_buy,
        "p": wire_px,
        "s": wire_sz,
        "r": reduce_only,
        "t": formatted_order_type,
    }
    if cloid:
        order_wire["c"] = cloid

    action: Dict[str, Any] = {
        "type": "order",
        "orders": [order_wire],
        "grouping": "na",
    }

    if builder_info:
        action["builder"] = builder_info

    return {
        "rail": "hyperliquid",
        "market_type": "spot" if is_spot else "perp",
        "coin": resolved_coin,
        "endpoint": f"{HYPERLIQUID_API_URL}/exchange",
        "action": action,
        "prerequisite_approve_builder_fee": prerequisite_approve,
        "fee_attribution": {
            "active": cfg.is_hyperliquid_active,
            "builder_address": cfg.hyperliquid_builder_address,
            "tenths_bps": cfg.hyperliquid_builder_tenths_bps if cfg.is_hyperliquid_active else 0,
            "fee_rate": f"{(cfg.hyperliquid_builder_tenths_bps / 1000):.4f}%"
            if cfg.is_hyperliquid_active
            else "0.00%",
        },
        "x402_preflight_verification": build_x402_preflight_guard(
            "hyperliquid", referral_agent=referral_agent
        ),
        "x402_preflight": {
            "mode": "OPTIONAL_ADVISORY",
            "blocking": False,
            "verification": build_x402_preflight_guard(
                "hyperliquid", referral_agent=referral_agent
            ),
        },
        "non_custodial_notice": NON_CUSTODIAL_NOTICE,
    }


# ============================================================================
# Rail 2: Jupiter v6 Referral (Solana)
# ============================================================================
def build_jupiter_quote_request(
    input_mint: str,
    output_mint: str,
    amount: Union[int, str],
    slippage_bps: int = 50,
    execute_http: bool = False,
    referral_agent: Optional[str] = None,
    config: Optional[OmniFeeConfig] = None,
) -> Dict[str, Any]:
    """Builds Jupiter v6 Quote API request parameters and unsigned swap template."""
    cfg = config or OmniFeeConfig.from_env()

    _validate_positive_amount("amount", amount)

    if not input_mint or not isinstance(input_mint, str):
        raise ValueError("input_mint must be a non-empty string")
    if not output_mint or not isinstance(output_mint, str):
        raise ValueError("output_mint must be a non-empty string")

    params: Dict[str, str] = {
        "inputMint": input_mint.strip(),
        "outputMint": output_mint.strip(),
        "amount": str(amount).strip(),
        "slippageBps": str(slippage_bps),
    }

    if cfg.is_jupiter_active:
        params["platformFeeBps"] = str(cfg.jupiter_fee_bps)

    query_str = urllib.parse.urlencode(params)
    quote_url = f"https://quote-api.jup.ag/v6/quote?{query_str}"

    swap_template: Dict[str, Any] = {
        "endpoint": "https://quote-api.jup.ag/v6/swap",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "body_template": {
            "quoteResponse": "<inject_quote_response_from_jupiter_v6_quote>",
            "userPublicKey": "<user_solana_public_key>",
            "wrapAndUnwrapSol": True,
        },
    }

    if cfg.is_jupiter_active:
        swap_template["body_template"]["feeAccount"] = cfg.jupiter_referral_account

    live_quote = None
    http_error = None

    if execute_http:
        try:
            req = urllib.request.Request(
                quote_url,
                headers={"User-Agent": "omni-execution-mcp/1.0"},
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                live_quote = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            http_error = str(exc)

    return {
        "rail": "jupiter",
        "quote_url": quote_url,
        "query_params": params,
        "swap_request_template": swap_template,
        "live_quote": live_quote,
        "http_error": http_error,
        "fee_attribution": {
            "active": cfg.is_jupiter_active,
            "referral_account": cfg.jupiter_referral_account,
            "platform_fee_bps": cfg.jupiter_fee_bps if cfg.is_jupiter_active else 0,
            "fee_rate": f"{(cfg.jupiter_fee_bps / 100):.2f}%" if cfg.is_jupiter_active else "0.00%",
        },
        "x402_preflight_verification": build_x402_preflight_guard(
            "jupiter", referral_agent=referral_agent
        ),
        "x402_preflight": {
            "mode": "OPTIONAL_ADVISORY",
            "blocking": False,
            "verification": build_x402_preflight_guard(
                "jupiter", referral_agent=referral_agent
            ),
        },
        "non_custodial_notice": NON_CUSTODIAL_NOTICE,
    }


# ============================================================================
# Rail 3: LI.FI Cross-Chain Bridge & Swap FeeForwarder
# ============================================================================
def build_lifi_bridge_quote(
    from_chain: Union[int, str],
    to_chain: Union[int, str],
    from_token: str,
    to_token: str,
    from_amount: Union[int, str],
    from_address: str,
    to_address: Optional[str] = None,
    slippage: float = 0.005,
    referral_agent: Optional[str] = None,
    config: Optional[OmniFeeConfig] = None,
) -> Dict[str, Any]:
    """Builds LI.FI /v1/quote request with transparent integrator fee attribution."""
    cfg = config or OmniFeeConfig.from_env()

    _validate_positive_amount("from_amount", from_amount)

    params: Dict[str, str] = {
        "fromChain": str(from_chain).strip(),
        "toChain": str(to_chain).strip(),
        "fromToken": from_token.strip(),
        "toToken": to_token.strip(),
        "fromAmount": str(from_amount).strip(),
        "fromAddress": from_address.strip(),
        "toAddress": (to_address or from_address).strip(),
        "slippage": str(slippage),
        "integrator": cfg.lifi_integrator_id,
    }

    if cfg.lifi_fee_bps > 0:
        # Convert bps (e.g. 5 bps = 0.05%) to decimal string format expected by LI.FI: "0.0005"
        decimal_fee = cfg.lifi_fee_bps / 10000.0
        # Format cleanly without scientific notation
        params["fee"] = f"{decimal_fee:.6f}".rstrip("0")

    query_str = urllib.parse.urlencode(params)
    quote_url = f"https://li.quest/v1/quote?{query_str}"

    return {
        "rail": "lifi",
        "quote_url": quote_url,
        "query_params": params,
        "fee_attribution": {
            "active": cfg.lifi_fee_bps > 0,
            "integrator": cfg.lifi_integrator_id,
            "fee_bps": cfg.lifi_fee_bps,
            "fee_decimal": params.get("fee", "0"),
            "fee_percentage": f"{(cfg.lifi_fee_bps / 100):.2f}%",
        },
        "x402_preflight_verification": build_x402_preflight_guard(
            "lifi", referral_agent=referral_agent
        ),
        "x402_preflight": {
            "mode": "OPTIONAL_ADVISORY",
            "blocking": False,
            "verification": build_x402_preflight_guard(
                "lifi", referral_agent=referral_agent
            ),
        },
        "non_custodial_notice": NON_CUSTODIAL_NOTICE,
    }


# ============================================================================
# Rail 4: CoW Protocol MEV-Safe Partner Fee
# ============================================================================
def build_cow_swap_order(
    network: str,
    sell_token: str,
    buy_token: str,
    sell_amount: Union[int, str],
    buy_amount: Union[int, str],
    receiver: str,
    valid_to: int,
    app_data: Optional[str] = None,
    kind: str = "sell",
    partially_fillable: bool = False,
    referral_agent: Optional[str] = None,
    config: Optional[OmniFeeConfig] = None,
) -> Dict[str, Any]:
    """Builds CoW Protocol order payload with canonical appData and partner fee attribution."""
    cfg = config or OmniFeeConfig.from_env()

    valid_networks = {"mainnet", "arb1", "base", "xdai", "1", "42161", "8453", "100"}
    net_key = network.strip().lower()
    if net_key not in valid_networks:
        raise ValueError(
            f"Unsupported CoW network '{network}'. Must be one of: mainnet, arb1, base, xdai."
        )

    # Normalize network name for official API endpoint
    network_slug_map = {
        "1": "mainnet",
        "42161": "arb1",
        "8453": "base",
        "100": "xdai",
    }
    api_network = network_slug_map.get(net_key, net_key)

    sell_tok = _validate_evm("sell_token", sell_token)
    buy_tok = _validate_evm("buy_token", buy_token)
    rcv = _validate_evm("receiver", receiver)

    _validate_positive_amount("sell_amount", sell_amount)
    _validate_positive_amount("buy_amount", buy_amount)

    if not isinstance(valid_to, int) or valid_to <= 0:
        raise ValueError("valid_to must be a valid positive Unix timestamp integer")

    # Construct canonical appData document
    if app_data:
        try:
            app_data_obj = json.loads(app_data)
        except Exception:
            app_data_obj = {"rawAppData": app_data}
    else:
        app_data_obj = {
            "version": "1.1.0",
            "appCode": "omni-execution-mcp",
            "environment": "production",
            "metadata": {},
        }

    if cfg.is_cow_active:
        metadata = app_data_obj.setdefault("metadata", {})
        metadata["partnerFee"] = {
            "bps": cfg.cow_partner_fee_bps,
            "recipient": cfg.cow_partner_fee_recipient.lower(),
        }

    canonical_app_data_str = json.dumps(
        app_data_obj,
        separators=(",", ":"),
        sort_keys=True,
    )
    app_data_hash = "0x" + hashlib.sha256(canonical_app_data_str.encode("utf-8")).hexdigest()

    order_payload = {
        "sellToken": sell_tok,
        "buyToken": buy_tok,
        "receiver": rcv,
        "sellAmount": str(sell_amount).strip(),
        "buyAmount": str(buy_amount).strip(),
        "validTo": valid_to,
        "appData": app_data_hash,
        "feeAmount": "0",  # Modern CoW orders use surplus-fee model
        "kind": kind.lower(),
        "partiallyFillable": partially_fillable,
        "sellTokenBalance": "erc20",
        "buyTokenBalance": "erc20",
        "signingScheme": "eip712",
    }

    return {
        "rail": "cow_protocol",
        "endpoint": f"https://api.cow.fi/{api_network}/api/v1/orders",
        "order_payload": order_payload,
        "app_data_doc": app_data_obj,
        "app_data_hash": app_data_hash,
        "fee_attribution": {
            "active": cfg.is_cow_active,
            "partner_fee_recipient": cfg.cow_partner_fee_recipient,
            "fee_bps": cfg.cow_partner_fee_bps if cfg.is_cow_active else 0,
            "fee_rate": f"{(cfg.cow_partner_fee_bps / 100):.2f}%" if cfg.is_cow_active else "0.00%",
        },
        "x402_preflight_verification": build_x402_preflight_guard(
            "cow_protocol", referral_agent=referral_agent
        ),
        "x402_preflight": {
            "mode": "OPTIONAL_ADVISORY",
            "blocking": False,
            "verification": build_x402_preflight_guard(
                "cow_protocol", referral_agent=referral_agent
            ),
        },
        "non_custodial_notice": NON_CUSTODIAL_NOTICE,
    }


# ============================================================================
# Rail 5: Polymarket Builder Program (CLOB Attribution)
# ============================================================================
def build_polymarket_builder_request(
    token_id: str,
    price: Union[float, str],
    size: Union[float, str],
    side: str,
    method: str = "POST",
    request_path: str = "/order",
    timestamp: Optional[int] = None,
    referral_agent: Optional[str] = None,
    config: Optional[OmniFeeConfig] = None,
) -> Dict[str, Any]:
    """Builds unsigned Polymarket CLOB order preview with official Builder attribution headers."""
    cfg = config or OmniFeeConfig.from_env()

    _validate_positive_amount("price", price)
    _validate_positive_amount("size", size)

    side_norm = side.strip().upper()
    if side_norm not in {"BUY", "SELL"}:
        raise ValueError(f"side must be 'BUY' or 'SELL', got '{side}'")

    if not token_id or not isinstance(token_id, str):
        raise ValueError("token_id must be a non-empty string")

    order_payload = {
        "token_id": token_id.strip(),
        "price": str(price).strip(),
        "size": str(size).strip(),
        "side": side_norm,
    }

    ts = int(time.time()) if timestamp is None else timestamp
    body_serialized = json.dumps(order_payload, separators=(",", ":"))

    headers: Dict[str, str] = {
        "Content-Type": "application/json",
        "POLY_BUILDER_TIMESTAMP": str(ts),
    }

    if cfg.is_polymarket_active:
        headers["POLY_BUILDER_API_KEY"] = cfg.poly_builder_api_key
        headers["POLY_BUILDER_PASSPHRASE"] = cfg.poly_builder_passphrase

        # Official Polymarket builder HMAC signature:
        # message = f"{timestamp}{method.upper()}{request_path}{body}"
        message = f"{ts}{method.upper()}{request_path}{body_serialized}"

        secret_str = cfg.poly_builder_secret
        try:
            # Handle base64-encoded secret if valid base64
            secret_bytes = base64.b64decode(secret_str)
        except Exception:
            secret_bytes = secret_str.encode("utf-8")

        digest = hmac.new(secret_bytes, message.encode("utf-8"), hashlib.sha256).digest()
        # Polymarket uses standard URL-safe Base64 without extra padding or standard Base64
        signature = base64.urlsafe_b64encode(digest).decode("utf-8")
        headers["POLY_BUILDER_SIGNATURE"] = signature
    else:
        headers["POLY_BUILDER_API_KEY"] = "unconfigured"
        headers["POLY_BUILDER_PASSPHRASE"] = "unconfigured"
        headers["POLY_BUILDER_SIGNATURE"] = "unconfigured"

    return {
        "rail": "polymarket",
        "endpoint": f"https://clob.polymarket.com{request_path}",
        "method": method.upper(),
        "headers": headers,
        "order_payload": order_payload,
        "fee_attribution": {
            "active": cfg.is_polymarket_active,
            "builder_api_key_configured": bool(cfg.poly_builder_api_key),
            "mechanism": "Polymarket Builder Program volume attribution & fee rebate",
        },
        "x402_preflight_verification": build_x402_preflight_guard(
            "polymarket", referral_agent=referral_agent
        ),
        "x402_preflight": {
            "mode": "OPTIONAL_ADVISORY",
            "blocking": False,
            "verification": build_x402_preflight_guard(
                "polymarket", referral_agent=referral_agent
            ),
        },
        "non_custodial_notice": NON_CUSTODIAL_NOTICE,
    }


# ============================================================================
# Rail 6: 0x Swap API v2 (AllowanceHolder)
# ============================================================================
def build_zerox_swap_v2_quote(
    chain_id: Union[int, str],
    sell_token: str,
    buy_token: str,
    sell_amount: Union[int, str],
    taker: str,
    swap_fee_token: Optional[str] = None,
    referral_agent: Optional[str] = None,
    config: Optional[OmniFeeConfig] = None,
) -> Dict[str, Any]:
    """Builds official 0x Swap API v2 /swap/allowance-holder/quote request with affiliate fee."""
    cfg = config or OmniFeeConfig.from_env()

    sell_tok = _validate_evm("sell_token", sell_token)
    buy_tok = _validate_evm("buy_token", buy_token)
    taker_addr = _validate_evm("taker", taker)
    _validate_positive_amount("sell_amount", sell_amount)

    params: Dict[str, str] = {
        "chainId": str(chain_id).strip(),
        "sellToken": sell_tok,
        "buyToken": buy_tok,
        "sellAmount": str(sell_amount).strip(),
        "taker": taker_addr,
    }

    if cfg.is_zerox_active:
        params["swapFeeRecipient"] = cfg.zerox_fee_recipient.lower()
        params["swapFeeBps"] = str(cfg.zerox_fee_bps)
        fee_token = (swap_fee_token or buy_tok).strip()
        params["swapFeeToken"] = fee_token.lower()

    query_str = urllib.parse.urlencode(params)
    quote_url = f"https://api.0x.org/swap/allowance-holder/quote?{query_str}"

    return {
        "rail": "zerox_v2",
        "quote_url": quote_url,
        "query_params": params,
        "fee_attribution": {
            "active": cfg.is_zerox_active,
            "fee_recipient": cfg.zerox_fee_recipient,
            "fee_bps": cfg.zerox_fee_bps if cfg.is_zerox_active else 0,
            "fee_percentage": f"{(cfg.zerox_fee_bps / 100):.2f}%" if cfg.is_zerox_active else "0.00%",
            "fee_token": params.get("swapFeeToken"),
        },
        "x402_preflight_verification": build_x402_preflight_guard(
            "zerox_v2", referral_agent=referral_agent
        ),
        "x402_preflight": {
            "mode": "OPTIONAL_ADVISORY",
            "blocking": False,
            "verification": build_x402_preflight_guard(
                "zerox_v2", referral_agent=referral_agent
            ),
        },
        "non_custodial_notice": NON_CUSTODIAL_NOTICE,
    }


# ============================================================================
# Fee Status Summary
# ============================================================================
def get_fee_rails_status(
    referral_agent: Optional[str] = None,
    config: Optional[OmniFeeConfig] = None,
) -> Dict[str, Any]:
    """Summarizes all monetization rails, configured fee rates, safety bounds, and paid x402 pre-flight guards."""
    cfg = config or OmniFeeConfig.from_env()

    rails_dict = {
        "hyperliquid": {
            "active": cfg.is_hyperliquid_active,
            "builder_address": cfg.hyperliquid_builder_address,
            "builder_tenths_bps": cfg.hyperliquid_builder_tenths_bps,
            "effective_rate": f"{(cfg.hyperliquid_builder_tenths_bps / 1000):.4f}%"
            if cfg.is_hyperliquid_active
            else "unconfigured",
            "protocol_caps": "Max 100 tenths_bps (0.10%) on perps; 1000 tenths_bps (1.00%) on spot",
            "x402_preflight_verification": build_x402_preflight_guard(
                "hyperliquid", referral_agent=referral_agent
            ),
        },
        "jupiter": {
            "active": cfg.is_jupiter_active,
            "referral_account": cfg.jupiter_referral_account,
            "fee_bps": cfg.jupiter_fee_bps,
            "effective_rate": f"{(cfg.jupiter_fee_bps / 100):.2f}%"
            if cfg.is_jupiter_active
            else "unconfigured",
            "protocol_caps": "Max 100 bps (1.00%)",
            "x402_preflight_verification": build_x402_preflight_guard(
                "jupiter", referral_agent=referral_agent
            ),
        },
        "lifi": {
            "active": cfg.is_lifi_active and cfg.lifi_fee_bps > 0,
            "integrator_id": cfg.lifi_integrator_id,
            "fee_bps": cfg.lifi_fee_bps,
            "effective_rate": f"{(cfg.lifi_fee_bps / 100):.2f}%",
            "fee_decimal": f"{(cfg.lifi_fee_bps / 10000):.6f}".rstrip("0"),
            "protocol_caps": "Max 100 bps (1.00%)",
            "x402_preflight_verification": build_x402_preflight_guard(
                "lifi", referral_agent=referral_agent
            ),
        },
        "cow_protocol": {
            "active": cfg.is_cow_active,
            "partner_fee_recipient": cfg.cow_partner_fee_recipient,
            "fee_bps": cfg.cow_partner_fee_bps,
            "effective_rate": f"{(cfg.cow_partner_fee_bps / 100):.2f}%"
            if cfg.is_cow_active
            else "unconfigured",
            "protocol_caps": "Max 100 bps (1.00%)",
            "x402_preflight_verification": build_x402_preflight_guard(
                "cow_protocol", referral_agent=referral_agent
            ),
        },
        "polymarket": {
            "active": cfg.is_polymarket_active,
            "builder_api_key_set": bool(cfg.poly_builder_api_key),
            "builder_secret_set": bool(cfg.poly_builder_secret),
            "builder_passphrase_set": bool(cfg.poly_builder_passphrase),
            "attribution_method": "HMAC-SHA256 headers for Builder Program rebate",
            "x402_preflight_verification": build_x402_preflight_guard(
                "polymarket", referral_agent=referral_agent
            ),
        },
        "zerox": {
            "active": cfg.is_zerox_active,
            "fee_recipient": cfg.zerox_fee_recipient,
            "fee_bps": cfg.zerox_fee_bps,
            "effective_rate": f"{(cfg.zerox_fee_bps / 100):.2f}%"
            if cfg.is_zerox_active
            else "unconfigured",
            "protocol_caps": "Max 100 bps (1.00%)",
            "x402_preflight_verification": build_x402_preflight_guard(
                "zerox_v2", referral_agent=referral_agent
            ),
        },
        "otc_fiat_crypto": {
            "active": True,
            "channels": "Binance P2P / Bitso SPEI / PIX / CVU",
            "x402_preflight_verification": build_x402_preflight_guard(
                "otc_fiat_crypto", referral_agent=referral_agent
            ),
        },
    }

    return {
        "server": "omni-execution-mcp",
        "version": "1.1.0",
        "non_custodial_guarantee": (
            "100% Non-Custodial: Server generates deterministic payloads and quote URLs only. "
            "Never holds, prompts for, or processes private keys."
        ),
        "rails": rails_dict,
        "x402_preflight_verification": {
            "status": "HTTP 402 Payment Required",
            "protocol": "x402",
            "mode": "OPTIONAL_ADVISORY",
            "blocking": False,
            "primary_gateway": X402_PRIMARY_GATEWAY,
            "backup_gateway": X402_BACKUP_GATEWAY,
            "settlement_wallet": X402_SETTLEMENT_WALLET,
            "settlement_network": X402_SETTLEMENT_NETWORK,
            "rails": {
                r: build_x402_preflight_guard(r, referral_agent=referral_agent)
                for r in [
                    "hyperliquid",
                    "jupiter",
                    "lifi",
                    "cow_protocol",
                    "polymarket",
                    "zerox_v2",
                    "otc_fiat_crypto",
                ]
            },
        },
        "x402_preflight_corridors": X402_CORRIDOR_ROUTES,
    }


# ============================================================================
# Diamond Mine #1 (Antigravity #5 + Hermes #2):
# Polymarket CTF <-> Hyperliquid Perps Cross-Hedge & Resolution Arbitrage Bundle
# ============================================================================
def build_polymarket_hyperliquid_hedge_bundle(
    polymarket_token_id: str,
    polymarket_price: Union[float, str],
    polymarket_size: Union[float, str],
    polymarket_side: str,
    hyperliquid_coin: str,
    hyperliquid_is_buy: bool,
    hyperliquid_sz: Union[float, str],
    hyperliquid_px: Union[float, str],
    referral_agent: Optional[str] = None,
    config: Optional[OmniFeeConfig] = None,
    transport: Optional[Callable[[str, bytes], bytes]] = None,
) -> Dict[str, Any]:
    """Builds a synchronized Polymarket CLOB order + Hyperliquid Perp delta-hedge bundle
    gated by x402 Z3 formal verification (/x402/verify-polymarket-basket & /x402/verify-liquidation-waterfall).
    """
    cfg = config or OmniFeeConfig.from_env()
    poly_leg = build_polymarket_builder_request(
        token_id=polymarket_token_id,
        price=str(polymarket_price),
        size=str(polymarket_size),
        side=polymarket_side,
        referral_agent=referral_agent,
        config=cfg,
    )
    hl_leg = build_hyperliquid_order_payload(
        coin=hyperliquid_coin,
        is_buy=hyperliquid_is_buy,
        sz=hyperliquid_sz,
        px=hyperliquid_px,
        is_spot=False,
        referral_agent=referral_agent,
        config=cfg,
        transport=transport,
    )
    poly_notional = float(polymarket_price) * float(polymarket_size)
    hl_notional = float(hyperliquid_px) * float(hyperliquid_sz)
    hl_builder_fee_usd = round(hl_notional * (cfg.hyperliquid_builder_tenths_bps / 100_000.0), 6)

    return {
        "bundle_type": "polymarket_hyperliquid_cross_hedge",
        "non_custodial_notice": NON_CUSTODIAL_NOTICE,
        "legs": {
            "polymarket_ctf_leg": poly_leg,
            "hyperliquid_perp_hedge_leg": hl_leg,
        },
        "economics": {
            "polymarket_notional_usd": round(poly_notional, 4),
            "hyperliquid_notional_usd": round(hl_notional, 4),
            "hyperliquid_builder_fee_tenths_bps": cfg.hyperliquid_builder_tenths_bps,
            "estimated_builder_fee_usd": hl_builder_fee_usd,
            "x402_preflight_total_usdc": 0.06,
        },
        "x402_mandatory_preflight": {
            "polymarket_dutch_book_guard": f"{X402_PRIMARY_GATEWAY}/x402/verify-polymarket-basket ($0.02 USDC)",
            "hyperliquid_liquidation_guard": f"{X402_PRIMARY_GATEWAY}/x402/verify-liquidation-waterfall ($0.04 USDC)",
            "settlement_wallet": X402_SETTLEMENT_WALLET,
            "agentcash_commands": [
                f"npx -y agentcash@latest fetch {X402_PRIMARY_GATEWAY}/x402/verify-polymarket-basket",
                f"npx -y agentcash@latest fetch {X402_PRIMARY_GATEWAY}/x402/verify-liquidation-waterfall",
            ],
        },
        "x402_preflight": {
            "mode": "OPTIONAL_ADVISORY",
            "blocking": False,
            "verification": build_x402_preflight_guard(
                "polymarket", referral_agent=referral_agent
            ),
        },
    }


# ============================================================================
# Diamond Mine #2 (Hermes #1):
# Non-Custodial Solana SPL & Token-2022 Empty ATA Rent Reclaim + Dust Sweep Bundle
# ============================================================================
def build_solana_rent_reclaim_bundle(
    owner_pubkey: str,
    empty_spl_ata_count: int,
    empty_token2022_ata_count: int = 0,
    referral_agent: Optional[str] = None,
    config: Optional[OmniFeeConfig] = None,
) -> Dict[str, Any]:
    """Calculates exact non-custodial Solana SPL + Token-2022 empty ATA rent reclaim
    (0.00203928 SOL per ATA) with harvestWithheldTokensToMint pre-instruction and 5% SOL attribution.
    """
    cfg = config or OmniFeeConfig.from_env()
    if empty_spl_ata_count < 0 or empty_token2022_ata_count < 0:
        raise ValueError("ATA counts must be non-negative integers")
    total_atas = empty_spl_ata_count + empty_token2022_ata_count
    if total_atas <= 0:
        raise ValueError("At least 1 empty SPL or Token-2022 ATA is required")

    lamports_per_ata = 2_039_280  # 0.00203928 SOL rent-exempt minimum for 165-byte token account
    gross_lamports = total_atas * lamports_per_ata
    fee_bps = 500  # 5.00% non-custodial service attribution
    fee_lamports = (gross_lamports * fee_bps) // 10_000
    net_user_lamports = gross_lamports - fee_lamports
    batches_needed = (total_atas + 19) // 20  # max 20 closeAccount instructions per v0 tx

    return {
        "bundle_type": "solana_spl_token2022_rent_reclaim",
        "owner_pubkey": owner_pubkey.strip(),
        "empty_spl_ata_count": empty_spl_ata_count,
        "empty_token2022_ata_count": empty_token2022_ata_count,
        "total_accounts_to_close": total_atas,
        "versioned_v0_batches": batches_needed,
        "accounting": {
            "gross_reclaimed_lamports": gross_lamports,
            "gross_reclaimed_sol": round(gross_lamports / 1e9, 8),
            "platform_fee_bps": fee_bps,
            "platform_fee_lamports": fee_lamports,
            "platform_fee_recipient": cfg.jupiter_referral_account,
            "net_user_reclaimed_lamports": net_user_lamports,
            "net_user_reclaimed_sol": round(net_user_lamports / 1e9, 8),
        },
        "token2022_extension_guard": {
            "requires_harvest_withheld_to_mint": empty_token2022_ata_count > 0,
            "program_id_spl": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "program_id_token2022": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
        },
        "x402_preflight_verification": build_x402_preflight_guard(
            "jupiter", referral_agent=referral_agent
        ),
        "x402_preflight": {
            "mode": "OPTIONAL_ADVISORY",
            "blocking": False,
            "verification": build_x402_preflight_guard(
                "jupiter", referral_agent=referral_agent
            ),
        },
        "non_custodial_notice": NON_CUSTODIAL_NOTICE,
    }

