"""Omni Execution MCP: Non-custodial DeFi/CeFi execution MCP server with transparent fee attribution and paid x402 pre-flight verification."""

from .config import OmniFeeConfig
from .rails import (
    NON_CUSTODIAL_NOTICE,
    build_cow_swap_order,
    build_hyperliquid_order_payload,
    build_jupiter_quote_request,
    build_lifi_bridge_quote,
    build_polymarket_builder_request,
    build_x402_preflight_guard,
    build_zerox_swap_v2_quote,
    get_fee_rails_status,
)
from .server import handle_jsonrpc_message, main

__version__ = "1.1.0"
__all__ = [
    "OmniFeeConfig",
    "NON_CUSTODIAL_NOTICE",
    "build_cow_swap_order",
    "build_hyperliquid_order_payload",
    "build_jupiter_quote_request",
    "build_lifi_bridge_quote",
    "build_polymarket_builder_request",
    "build_x402_preflight_guard",
    "build_zerox_swap_v2_quote",
    "get_fee_rails_status",
    "handle_jsonrpc_message",
    "main",
]
