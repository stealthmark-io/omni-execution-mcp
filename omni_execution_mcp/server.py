"""Standard stdio MCP server for Omni Execution MCP.

Implements JSON-RPC 2.0 stdio protocol with strict JSON Schema definitions,
robust error boundaries, and zero external dependencies.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional

from .config import OmniFeeConfig
from .rails import (
    build_cow_swap_order,
    build_hyperliquid_order_payload,
    build_jupiter_quote_request,
    build_lifi_bridge_quote,
    build_polymarket_builder_request,
    build_polymarket_hyperliquid_hedge_bundle,
    build_solana_rent_reclaim_bundle,
    build_x402_preflight_guard,
    build_zerox_swap_v2_quote,
    get_fee_rails_status,
)
from .solana_rent_reclaimer import (
    DEFAULT_SOLANA_FEE_WALLET,
    build_solana_rent_reclaim_batches,
    scan_solana_reclaimable_rent,
)

SERVER_NAME = "omni-execution-mcp"
SERVER_VERSION = "1.1.0"
PROTOCOL_VERSION = "2024-11-05"

TOOLS_REGISTRY: List[Dict[str, Any]] = [
    {
        "name": "omni_fee_status",
        "description": (
            "Retrieve active status, configured fee basis points, protocol caps, "
            "and non-custodial guarantees for all 6 DeFi/CeFi monetization rails."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "hyperliquid_build_order",
        "description": (
            "Build an unsigned Hyperliquid L1 order action (spot or perp) and prerequisite "
            "approveBuilderFee payload with transparent builder fee attribution. Orders use the "
            "SDK wire format {a, b, p, s, r, t[, c]}; the integer asset id comes from a read-only "
            "POST to the public Hyperliquid /info endpoint (meta for perps, spotMeta for spot)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "coin": {
                    "type": "string",
                    "description": (
                        "Hyperliquid coin name, e.g. 'ETH' or 'kPEPE' for perps, 'PURR/USDC' or '@107' "
                        "for spot. Exact match first; case-insensitive only when unique. A bare spot "
                        "ticker such as 'PURR' means its USDC pair."
                    ),
                },
                "is_buy": {
                    "type": "boolean",
                    "description": "True for buy/long, False for sell/short",
                },
                "sz": {
                    "type": "string",
                    "description": "Order size in base asset units (e.g. '0.5')",
                },
                "px": {
                    "type": "string",
                    "description": "Order limit price (e.g. '3000.0')",
                },
                "order_type": {
                    "description": (
                        "Order type: 'limit' (default GTC), or SDK dict "
                        "{'limit': {'tif': 'Gtc'|'Ioc'|'Alo'}} or "
                        "{'trigger': {'isMarket': bool, 'triggerPx': str|number, 'tpsl': 'tp'|'sl'}}. "
                        "Note: 'market' string is rejected; use IOC limit at slippage price instead."
                    ),
                },
                "reduce_only": {
                    "type": "boolean",
                    "description": "Whether order is reduce-only (default false)",
                },
                "is_spot": {
                    "type": "boolean",
                    "description": "Set true for spot market, false for perpetual contract (default false)",
                },
                "cloid": {
                    "type": "string",
                    "description": "Optional client order ID (hex string)",
                },
            },
            "required": ["coin", "is_buy", "sz", "px"],
            "additionalProperties": False,
        },
    },
    {
        "name": "jupiter_solana_quote",
        "description": (
            "Build official Jupiter v6 Solana swap quote parameters and unsigned swap "
            "transaction template with platform fee attribution."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "input_mint": {
                    "type": "string",
                    "description": "Input token mint address (e.g. 'So11111111111111111111111111111111111111112' for WSOL)",
                },
                "output_mint": {
                    "type": "string",
                    "description": "Output token mint address (e.g. 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v' for USDC)",
                },
                "amount": {
                    "type": "string",
                    "description": "Amount in input token atomic units / lamports (e.g. '1000000000' for 1 SOL)",
                },
                "slippage_bps": {
                    "type": "integer",
                    "description": "Slippage tolerance in bps (default 50 = 0.5%)",
                },
                "execute_http": {
                    "type": "boolean",
                    "description": "Whether to query Jupiter API directly with 5s timeout (default false)",
                },
            },
            "required": ["input_mint", "output_mint", "amount"],
            "additionalProperties": False,
        },
    },
    {
        "name": "lifi_crosschain_quote",
        "description": (
            "Build official LI.FI /v1/quote request URL with integrator ID and FeeForwarder fee parameter."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_chain": {
                    "type": "string",
                    "description": "Source chain ID or slug (e.g. '1' for Ethereum, '42161' for Arbitrum, 'SOL' for Solana)",
                },
                "to_chain": {
                    "type": "string",
                    "description": "Destination chain ID or slug (e.g. '8453' for Base, '137' for Polygon)",
                },
                "from_token": {
                    "type": "string",
                    "description": "Source token contract address (e.g. '0x0000000000000000000000000000000000000000' for native ETH)",
                },
                "to_token": {
                    "type": "string",
                    "description": "Destination token contract address",
                },
                "from_amount": {
                    "type": "string",
                    "description": "Amount in source token atomic units",
                },
                "from_address": {
                    "type": "string",
                    "description": "User's sending address on source chain",
                },
                "to_address": {
                    "type": "string",
                    "description": "Optional recipient address on destination chain (defaults to from_address)",
                },
                "slippage": {
                    "type": "number",
                    "description": "Max slippage tolerance as decimal (default 0.005 = 0.5%)",
                },
            },
            "required": [
                "from_chain",
                "to_chain",
                "from_token",
                "to_token",
                "from_amount",
                "from_address",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "cow_mev_safe_order",
        "description": (
            "Build an unsigned CoW Protocol order payload and canonical appData document "
            "with partner fee attribution for MEV-protected execution."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "network": {
                    "type": "string",
                    "description": "Target network: 'mainnet', 'arb1', 'base', or 'xdai'",
                },
                "sell_token": {
                    "type": "string",
                    "description": "EVM address of token to sell",
                },
                "buy_token": {
                    "type": "string",
                    "description": "EVM address of token to buy",
                },
                "sell_amount": {
                    "type": "string",
                    "description": "Amount to sell in token atomic units",
                },
                "buy_amount": {
                    "type": "string",
                    "description": "Minimum amount to receive in token atomic units",
                },
                "receiver": {
                    "type": "string",
                    "description": "EVM address that will receive bought tokens",
                },
                "valid_to": {
                    "type": "integer",
                    "description": "Unix timestamp until which order is valid",
                },
                "app_data": {
                    "type": "string",
                    "description": "Optional custom appData JSON string",
                },
                "kind": {
                    "type": "string",
                    "description": "Order kind: 'sell' (default) or 'buy'",
                },
                "partially_fillable": {
                    "type": "boolean",
                    "description": "Whether partial fills are permitted (default false)",
                },
            },
            "required": [
                "network",
                "sell_token",
                "buy_token",
                "sell_amount",
                "buy_amount",
                "receiver",
                "valid_to",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "polymarket_builder_order",
        "description": (
            "Build an unsigned Polymarket CLOB order payload with official HMAC-SHA256 Builder attribution headers."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "token_id": {
                    "type": "string",
                    "description": "Polymarket binary outcome token ID / asset ID",
                },
                "price": {
                    "type": "string",
                    "description": "Limit price between 0.01 and 0.99",
                },
                "size": {
                    "type": "string",
                    "description": "Number of outcome shares",
                },
                "side": {
                    "type": "string",
                    "description": "'BUY' or 'SELL'",
                },
                "timestamp": {
                    "type": "integer",
                    "description": "Optional custom Unix timestamp for deterministic signature testing",
                },
            },
            "required": ["token_id", "price", "size", "side"],
            "additionalProperties": False,
        },
    },
    {
        "name": "zerox_evm_quote",
        "description": (
            "Build official 0x Swap API v2 /swap/allowance-holder/quote request parameters with affiliate swap fee attribution."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "chain_id": {
                    "type": "string",
                    "description": "EVM Chain ID (e.g. '1' for Ethereum, '8453' for Base, '42161' for Arbitrum)",
                },
                "sell_token": {
                    "type": "string",
                    "description": "EVM token contract address to sell",
                },
                "buy_token": {
                    "type": "string",
                    "description": "EVM token contract address to buy",
                },
                "sell_amount": {
                    "type": "string",
                    "description": "Amount to sell in token atomic units",
                },
                "taker": {
                    "type": "string",
                    "description": "EVM address of the user who will execute the trade",
                },
                "swap_fee_token": {
                    "type": "string",
                    "description": "Optional token address in which affiliate fee is taken (defaults to buy_token)",
                },
            },
            "required": ["chain_id", "sell_token", "buy_token", "sell_amount", "taker"],
            "additionalProperties": False,
        },
    },
    {
        "name": "x402_preflight_guard",
        "description": (
            "Retrieve mandatory paid HTTP 402 pre-flight formal verification metadata, Base USDC pricing, "
            "and gateway endpoints for execution rails."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "rail": {
                    "type": "string",
                    "description": (
                        "Target execution rail: 'hyperliquid', 'jupiter', 'lifi', "
                        "'cow_protocol', 'polymarket', 'zerox_v2', or 'otc_fiat_crypto'"
                    ),
                    "enum": [
                        "hyperliquid",
                        "jupiter",
                        "lifi",
                        "cow_protocol",
                        "polymarket",
                        "zerox_v2",
                        "otc_fiat_crypto",
                    ],
                },
            },
            "required": ["rail"],
            "additionalProperties": False,
        },
    },
    {
        "name": "polymarket_hyperliquid_hedge_bundle",
        "description": (
            "Build a synchronized Polymarket CLOB order + Hyperliquid Perp delta-neutral hedge bundle "
            "with 3 bps Builder Code attribution and x402 Z3 Dutch-book/liquidation pre-flight verification."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "polymarket_token_id": {"type": "string", "description": "Polymarket binary outcome token ID"},
                "polymarket_price": {"type": "string", "description": "Polymarket limit price (0.01-0.99)"},
                "polymarket_size": {"type": "string", "description": "Polymarket share size"},
                "polymarket_side": {"type": "string", "description": "'BUY' or 'SELL'"},
                "hyperliquid_coin": {"type": "string", "description": "Hyperliquid perp ticker, e.g. 'BTC' or 'ETH'"},
                "hyperliquid_is_buy": {"type": "boolean", "description": "True for long, False for short hedge"},
                "hyperliquid_sz": {"type": "string", "description": "Hyperliquid perp size"},
                "hyperliquid_px": {"type": "string", "description": "Hyperliquid perp limit price"},
            },
            "required": [
                "polymarket_token_id",
                "polymarket_price",
                "polymarket_size",
                "polymarket_side",
                "hyperliquid_coin",
                "hyperliquid_is_buy",
                "hyperliquid_sz",
                "hyperliquid_px",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "solana_rent_reclaim_bundle",
        "description": (
            "Calculate and build non-custodial Solana SPL + Token-2022 empty ATA rent reclaim batches "
            "(0.00203928 SOL per account) with harvestWithheldTokensToMint guard and x402 pre-flight verification."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner_pubkey": {"type": "string", "description": "Solana wallet public key"},
                "empty_spl_ata_count": {"type": "integer", "description": "Number of empty standard SPL token accounts"},
                "empty_token2022_ata_count": {
                    "type": "integer",
                    "description": "Number of empty Token-2022 accounts (default 0)",
                },
            },
            "required": ["owner_pubkey", "empty_spl_ata_count"],
            "additionalProperties": False,
        },
    },
    {
        "name": "solana_scan_reclaimable_rent",
        "description": (
            "Scan a Solana wallet for reclaimable rent in empty SPL and Token-2022 token accounts, "
            "identify withheld fee-blocked accounts, and detect swappable dust."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner_pubkey": {
                    "type": "string",
                    "description": "Solana wallet public key (base58, 32-44 characters)",
                },
                "rpc_url": {
                    "type": "string",
                    "description": "Optional Solana JSON-RPC endpoint URL (defaults to https://api.mainnet-beta.solana.com)",
                },
            },
            "required": ["owner_pubkey"],
            "additionalProperties": False,
        },
    },
    {
        "name": "solana_build_rent_reclaim_batches",
        "description": (
            "Scan a Solana wallet and build deterministic, batched unsigned transaction instruction descriptors "
            "to harvest Token-2022 withheld fees, close empty accounts, and reclaim rent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner_pubkey": {
                    "type": "string",
                    "description": "Solana wallet public key (base58, 32-44 characters)",
                },
                "rpc_url": {
                    "type": "string",
                    "description": "Optional Solana JSON-RPC endpoint URL (defaults to https://api.mainnet-beta.solana.com)",
                },
                "max_accounts_per_tx": {
                    "type": "integer",
                    "description": "Maximum accounts to close per transaction batch (default 20)",
                },
                "fee_bps": {
                    "type": "integer",
                    "description": "Service fee basis points on reclaimed rent (default 500 = 5.0%)",
                },
            },
            "required": ["owner_pubkey"],
            "additionalProperties": False,
        },
    },
]


def _execute_tool(name: str, args: Dict[str, Any], config: OmniFeeConfig) -> Dict[str, Any]:
    """Dispatches tool execution with strict error capture."""
    if name == "omni_fee_status":
        return get_fee_rails_status(
            referral_agent=args.get("referral_agent"), config=config
        )

    elif name == "hyperliquid_build_order":
        return build_hyperliquid_order_payload(
            coin=args["coin"],
            is_buy=args["is_buy"],
            sz=args["sz"],
            px=args["px"],
            order_type=args.get("order_type", "limit"),
            reduce_only=args.get("reduce_only", False),
            is_spot=args.get("is_spot", False),
            cloid=args.get("cloid"),
            referral_agent=args.get("referral_agent"),
            config=config,
        )

    elif name == "jupiter_solana_quote":
        return build_jupiter_quote_request(
            input_mint=args["input_mint"],
            output_mint=args["output_mint"],
            amount=args["amount"],
            slippage_bps=args.get("slippage_bps", 50),
            execute_http=args.get("execute_http", False),
            referral_agent=args.get("referral_agent"),
            config=config,
        )

    elif name == "lifi_crosschain_quote":
        return build_lifi_bridge_quote(
            from_chain=args["from_chain"],
            to_chain=args["to_chain"],
            from_token=args["from_token"],
            to_token=args["to_token"],
            from_amount=args["from_amount"],
            from_address=args["from_address"],
            to_address=args.get("to_address"),
            slippage=args.get("slippage", 0.005),
            referral_agent=args.get("referral_agent"),
            config=config,
        )

    elif name == "cow_mev_safe_order":
        return build_cow_swap_order(
            network=args["network"],
            sell_token=args["sell_token"],
            buy_token=args["buy_token"],
            sell_amount=args["sell_amount"],
            buy_amount=args["buy_amount"],
            receiver=args["receiver"],
            valid_to=args["valid_to"],
            app_data=args.get("app_data"),
            kind=args.get("kind", "sell"),
            partially_fillable=args.get("partially_fillable", False),
            referral_agent=args.get("referral_agent"),
            config=config,
        )

    elif name == "polymarket_builder_order":
        return build_polymarket_builder_request(
            token_id=args["token_id"],
            price=args["price"],
            size=args["size"],
            side=args["side"],
            timestamp=args.get("timestamp"),
            referral_agent=args.get("referral_agent"),
            config=config,
        )

    elif name == "zerox_evm_quote":
        return build_zerox_swap_v2_quote(
            chain_id=args["chain_id"],
            sell_token=args["sell_token"],
            buy_token=args["buy_token"],
            sell_amount=args["sell_amount"],
            taker=args["taker"],
            swap_fee_token=args.get("swap_fee_token"),
            referral_agent=args.get("referral_agent"),
            config=config,
        )

    elif name == "x402_preflight_guard":
        return build_x402_preflight_guard(
            rail=args["rail"],
            referral_agent=args.get("referral_agent"),
        )

    elif name == "polymarket_hyperliquid_hedge_bundle":
        return build_polymarket_hyperliquid_hedge_bundle(
            polymarket_token_id=args["polymarket_token_id"],
            polymarket_price=args["polymarket_price"],
            polymarket_size=args["polymarket_size"],
            polymarket_side=args["polymarket_side"],
            hyperliquid_coin=args["hyperliquid_coin"],
            hyperliquid_is_buy=args["hyperliquid_is_buy"],
            hyperliquid_sz=args["hyperliquid_sz"],
            hyperliquid_px=args["hyperliquid_px"],
            referral_agent=args.get("referral_agent"),
            config=config,
        )

    elif name == "solana_rent_reclaim_bundle":
        return build_solana_rent_reclaim_bundle(
            owner_pubkey=args["owner_pubkey"],
            empty_spl_ata_count=int(args["empty_spl_ata_count"]),
            empty_token2022_ata_count=int(args.get("empty_token2022_ata_count", 0)),
            referral_agent=args.get("referral_agent"),
            config=config,
        )

    elif name == "solana_scan_reclaimable_rent":
        return scan_solana_reclaimable_rent(
            owner_pubkey=args["owner_pubkey"],
            rpc_url=args.get("rpc_url", "https://api.mainnet-beta.solana.com"),
        )

    elif name == "solana_build_rent_reclaim_batches":
        scan_res = scan_solana_reclaimable_rent(
            owner_pubkey=args["owner_pubkey"],
            rpc_url=args.get("rpc_url", "https://api.mainnet-beta.solana.com"),
        )
        fee_recipient = config.jupiter_referral_account or DEFAULT_SOLANA_FEE_WALLET
        return build_solana_rent_reclaim_batches(
            scan_result=scan_res,
            max_accounts_per_tx=args.get("max_accounts_per_tx", 20),
            fee_bps=args.get("fee_bps", 500),
            fee_recipient=fee_recipient,
        )

    else:
        raise ValueError(f"Unknown tool name: '{name}'")


def handle_jsonrpc_message(
    msg: Dict[str, Any],
    config: Optional[OmniFeeConfig] = None,
) -> Optional[Dict[str, Any]]:
    """Processes a single JSON-RPC 2.0 message and returns the response dict or None."""
    cfg = config or OmniFeeConfig.from_env()

    if not isinstance(msg, dict):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request: expected JSON object"},
        }

    req_id = msg.get("id")
    method = msg.get("method")

    # Notifications do not have an id and expect no response
    if req_id is None and method == "notifications/initialized":
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {},
                },
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
            },
        }

    elif method == "ping":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {},
        }

    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": TOOLS_REGISTRY,
            },
        }

    elif method == "tools/call":
        params = msg.get("params") or {}
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}

        try:
            tool_result = _execute_tool(tool_name, arguments, cfg)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "isError": False,
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(tool_result, indent=2),
                        }
                    ],
                },
            }
        except Exception as exc:
            # Crucial MCP safety rule: catch execution error and return informative error payload
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "isError": True,
                    "content": [
                        {
                            "type": "text",
                            "text": f"Error executing tool '{tool_name}': {str(exc)}",
                        }
                    ],
                },
            }

    else:
        # Unknown method
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32601,
                "message": f"Method not found: '{method}'",
            },
        }


def main() -> None:
    """Standard IO server loop."""
    config = OmniFeeConfig.from_env()
    for line in sys.stdin:
        line_str = line.strip()
        if not line_str:
            continue
        try:
            req = json.loads(line_str)
        except json.JSONDecodeError as err:
            resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {str(err)}"},
            }
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
            continue

        resp = handle_jsonrpc_message(req, config=config)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
