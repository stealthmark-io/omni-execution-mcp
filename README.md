# Omni Execution MCP (`v1.1.0`)

A production-grade, 100% non-custodial Model Context Protocol (MCP) server providing deterministic execution payloads, official quote aggregations, transparent integrator fee attribution, and strict paid-only **HTTP 402 Base USDC pre-flight verification guards** across major DeFi and CeFi execution rails.

Built strictly with Python 3.10+ standard library (zero external pip dependencies required).

---

## Non-Custodial Architecture

`omni-execution-mcp` is designed from first principles with **zero custody** and **zero private key access**:

1. **No Key Storage or Handling**: The MCP server never accepts, requires, or stores private keys or seed phrases.
2. **Deterministic Payload Construction**: All actions returned by this server are unsigned transaction payloads (EIP-712 typed data, L1 actions, or official quote URLs).
3. **Client-Side Signature**: The client application, desktop agent, or connected wallet (e.g., MetaMask, Rabby, Phantom, Ledger) signs and broadcasts the transaction directly.
4. **Transparent Fee Attribution**: Integrator builder codes, referral accounts, and partner basis points are transparently included in the generated payloads and disclosed in response metadata.
5. **Strict Paid-Only x402 Pre-Flight Guards**: Every rail returns required HTTP 402 formal verification metadata on Base USDC (`eip155:8453`).

---

## The Execution Rails & x402 Pre-Flight Guards

| Rail # | Protocol | Asset / Venue | Attribution Mechanism | Default Fee | Protocol Cap | Required x402 Verifications | Total Verification Cost |
|---|---|---|---|---|---|---|---|
| **1** | **Hyperliquid** | L1 Perps & Spot DEX | Builder Code (`builder: {b, f}`) + `approveBuilderFee` | 3 bps (`0.03%`) | 10 bps perps / 100 bps spot | `/x402/verify-liquidation-waterfall` ($0.04)<br>`/x402/verify-mev-sandwich-bound` ($0.02) | **$0.06 USDC** |
| **2** | **Jupiter v6** | Solana DEX Aggregation | `platformFeeBps` query param + `feeAccount` in `/v6/swap` | 5 bps (`0.05%`) | 100 bps (`1.00%`) | `/x402/verify-mev-sandwich-bound` ($0.02)<br>`/x402/verify-amm-constant-product` ($0.03) | **$0.05 USDC** |
| **3** | **LI.FI** | Cross-Chain Bridges | FeeForwarder (`integrator` + `fee` decimal param) | 5 bps (`0.05%`) | 100 bps (`1.00%`) | `/x402/verify-crosschain-bridge-memo` ($0.02)<br>`/x402/verify-forensic-graph` ($0.05) | **$0.07 USDC** |
| **4** | **CoW Protocol** | MEV-Protected Batch Auctions | Canonical `appData` JSON (`metadata.partnerFee`) | 5 bps (`0.05%`) | 100 bps (`1.00%`) | `/x402/verify-mev-sandwich-bound` ($0.02)<br>`/x402/verify-permit2-signature-scope` ($0.02) | **$0.04 USDC** |
| **5** | **Polymarket** | Prediction Market CLOB | Builder Program HMAC-SHA256 headers | Volume Rebate | N/A | `/x402/verify-polymarket-basket` ($0.02) | **$0.02 USDC** |
| **6** | **0x Swap API v2** | EVM DEX Aggregation | AllowanceHolder `swapFeeBps` & `swapFeeRecipient` | 5 bps (`0.05%`) | 100 bps (`1.00%`) | `/x402/verify-permit2-signature-scope` ($0.02)<br>`/x402/verify-amm-constant-product` ($0.03) | **$0.05 USDC** |
| **7** | **OTC Fiat-Crypto** | Binance P2P / Bitso SPEI / PIX / CVU | Merchant & Escrow Settlement | N/A | N/A | `/x402/verify-fiat-crypto-otc-settlement` ($0.05)<br>`/x402/verify-forensic-graph` ($0.05) | **$0.10 USDC** |

---

## x402 Settlement & Gateways

- **Settlement Chain**: Base Mainnet USDC (`eip155:8453`)
- **Settlement Wallet**: `0xc8dc4ecb93c4c665Fad07E1261F5f7D60e3C6E0F`
- **Gateways**: Primary `https://gateway.stealthmark.io`, Backup `https://sentinel.oceaneye-ai.com`

---

## Available MCP Tools

The server exposes 8 standard tools implementing strict JSON Schema contracts:

1. `omni_fee_status`: Inspect configured fee basis points, protocol caps, active rail status, and x402 pre-flight verification metadata.
2. `hyperliquid_build_order`: Generate unsigned Hyperliquid L1 order payloads and `approveBuilderFee` actions with paid x402 guard.
3. `jupiter_solana_quote`: Generate Jupiter v6 quote URLs with referral fee parameters, unsigned swap templates, and paid x402 guard.
4. `lifi_crosschain_quote`: Generate LI.FI `/v1/quote` cross-chain bridge parameters with integrator FeeForwarder fee and paid x402 guard.
5. `cow_mev_safe_order`: Generate unsigned CoW Protocol order payloads and canonical `appData` JSON with MEV-safe partner fees and paid x402 guard.
6. `polymarket_builder_order`: Generate unsigned Polymarket CLOB order previews with HMAC-SHA256 Builder Program headers and paid x402 guard.
7. `zerox_evm_quote`: Generate 0x Swap API v2 `/swap/allowance-holder/quote` parameters with affiliate swap fees and paid x402 guard.
8. `x402_preflight_guard`: Query complete paid x402 pre-flight specification, Base USDC pricing, verification endpoints, and example payload for any rail.

---

## Quickstart & Setup

### 1. Local Run (Zero pip dependencies)

```bash
# Clone and enter directory
cd omni-execution-mcp

# Copy environment variable template
cp .env.example .env

# Run stdio MCP server directly
python3 -m omni_execution_mcp.server
```

### 2. Configuration in Claude Desktop / Code

Add the following entry to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "omni-execution-mcp": {
      "command": "python3",
      "args": [
        "-m",
        "omni_execution_mcp.server"
      ],
      "cwd": "/path/to/omni-execution-mcp",
      "env": {
        "HYPERLIQUID_BUILDER_ADDRESS": "0x0000000000000000000000000000000000000000",
        "HYPERLIQUID_BUILDER_TENTHS_BPS": "30",
        "JUPITER_REFERRAL_ACCOUNT": "11111111111111111111111111111111",
        "JUPITER_FEE_BPS": "5",
        "LIFI_INTEGRATOR_ID": "omni-execution-mcp",
        "LIFI_FEE_BPS": "5",
        "COW_PARTNER_FEE_RECIPIENT": "0x0000000000000000000000000000000000000000",
        "COW_PARTNER_FEE_BPS": "5",
        "ZEROX_FEE_RECIPIENT": "0x0000000000000000000000000000000000000000",
        "ZEROX_FEE_BPS": "5"
      }
    }
  }
}
```

### 3. Configuration in Cursor

In Cursor Settings -> Features -> MCP -> Add New MCP Server:
- **Name**: `omni-execution`
- **Type**: `command`
- **Command**: `python3 -m omni_execution_mcp.server`

### 4. Configuration in Hermes / Smithery

The repository includes `smithery.yaml` and `mcp.json` ready for automatic discovery on Smithery, Glama, and mcp.so registries.

---

## Testing

Run the full unit test suite with standard library `unittest`:

```bash
python3 -m unittest discover -s tests -v
```
