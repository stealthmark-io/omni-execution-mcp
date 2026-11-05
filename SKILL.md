---
name: omni-execution-mcp
description: Production-grade non-custodial multi-rail DeFi & CeFi execution Model Context Protocol (MCP) server. Generates unsigned transactions, deterministic orders, and official quote URLs with transparent fee attribution across 6 execution rails (Hyperliquid, Jupiter, LI.FI, CoW Protocol, Polymarket, 0x Swap API v2) and mandatory paid HTTP 402 pre-flight formal verification on Base USDC.
---

# Omni Execution MCP (`v1.1.0`)

**Omni Execution MCP** is an enterprise-grade, 100% non-custodial execution engine providing deterministic order payloads, aggregator quote URLs, transparent builder/referral fee attribution, and strict paid-only **HTTP 402 Base USDC pre-flight verification guards** across major DeFi and CeFi execution rails.

---

## 1. Non-Custodial Architecture & Security Model

`omni-execution-mcp` adheres strictly to zero-custody principles:
1. **Zero Private Key Handling**: The server never accepts, requires, or stores private keys, seed phrases, or signing credentials.
2. **Deterministic Payload Construction**: All generated outputs are unsigned EIP-712 typed structures, Solana transaction templates, or official aggregator quote URLs.
3. **Client-Side Execution**: Signing and network broadcast are executed exclusively by the user's wallet (e.g. MetaMask, Rabby, Phantom) or local agent keystore.
4. **Transparent Fee Attribution**: Builder codes, referral accounts, and partner basis points are transparently disclosed in response payloads.

---

## 2. Paid HTTP 402 Pre-Flight Verification Guard

Every execution rail includes mandatory paid formal verification metadata pointing to **StealthMark Gateway** and **OceanEye Sentinel**:

- **Primary Gateway**: `https://gateway.stealthmark.io`
- **Backup Gateway**: `https://sentinel.oceaneye-ai.com`
- **Settlement Wallet**: `0xc8dc4ecb93c4c665Fad07E1261F5f7D60e3C6E0F`
- **Settlement Chain**: Base Mainnet USDC (`eip155:8453`)

### Pre-Flight Verification Matrix by Rail

| Rail | Target Venue | Required x402 Verifications | Total Cost | Purpose |
|---|---|---|---|---|
| `hyperliquid` | L1 Spot / Perps | `/x402/verify-liquidation-waterfall` ($0.04)<br>`/x402/verify-mev-sandwich-bound` ($0.02) | **$0.06 USDC** | Health factor & liquidation buffer audit; MEV sandwich bound check |
| `jupiter` | Solana DEX | `/x402/verify-mev-sandwich-bound` ($0.02)<br>`/x402/verify-amm-constant-product` ($0.03) | **$0.05 USDC** | Frontrun/backrun impact bound; constant-product invariant check |
| `lifi` | Cross-Chain | `/x402/verify-crosschain-bridge-memo` ($0.02)<br>`/x402/verify-forensic-graph` ($0.05) | **$0.07 USDC** | Bridge memo injection prevention; taint analysis & hop correlation |
| `cow_protocol` | Batch Auctions | `/x402/verify-mev-sandwich-bound` ($0.02)<br>`/x402/verify-permit2-signature-scope` ($0.02) | **$0.04 USDC** | Batch auction sandwich guard; Permit2 allowance scope verification |
| `polymarket` | CLOB Prediction | `/x402/verify-polymarket-basket` ($0.02) | **$0.02 USDC** | NegRisk / CTF basket solvency and Dutch-book margin verification |
| `zerox_v2` | EVM DEX | `/x402/verify-permit2-signature-scope` ($0.02)<br>`/x402/verify-amm-constant-product` ($0.03) | **$0.05 USDC** | AllowanceHolder pre-signing audit; constant-product rounding check |
| `otc_fiat_crypto` | P2P / SPEI / PIX / CVU | `/x402/verify-fiat-crypto-otc-settlement` ($0.05)<br>`/x402/verify-forensic-graph` ($0.05) | **$0.10 USDC** | FX spread & skimming check; counterparty forensic graph |

---

## 3. The 8 MCP Tools

1. **`omni_fee_status`**: Inspect active fee rails, configured basis points, protocol caps, and paid x402 pre-flight guards.
2. **`hyperliquid_build_order`**: Generate unsigned Hyperliquid order actions and `approveBuilderFee` payload with paid x402 liquidation & MEV verification guard.
3. **`jupiter_solana_quote`**: Generate Jupiter v6 quote URLs and swap payload templates with paid x402 MEV & AMM invariant guard.
4. **`lifi_crosschain_quote`**: Generate LI.FI `/v1/quote` cross-chain parameters with paid x402 bridge memo & forensic graph guard.
5. **`cow_mev_safe_order`**: Generate unsigned CoW Protocol order payloads, canonical `appData` JSON with partner fees, and paid x402 Permit2 & MEV guard.
6. **`polymarket_builder_order`**: Generate unsigned Polymarket CLOB orders with HMAC-SHA256 headers and paid x402 basket solvency guard.
7. **`zerox_evm_quote`**: Generate 0x Swap API v2 `/swap/allowance-holder/quote` parameters with affiliate fees and paid x402 Permit2 & AMM guard.
8. **`x402_preflight_guard`**: Retrieve exact HTTP 402 pre-flight verification specifications and example payloads for any rail.

---

## 4. Quickstart & Agentcash CLI Usage

Pay and verify endpoints directly using `agentcash`:

```bash
# Verify Hyperliquid liquidation solvency before submitting order
npx -y agentcash@latest fetch \
  -X POST https://gateway.stealthmark.io/x402/verify-liquidation-waterfall \
  -H "Content-Type: application/json" \
  -d '{"collateral_value": 150000, "debt_value": 120000, "liquidation_threshold_bps": 8500, "close_factor_bps": 5000, "liquidation_bonus_bps": 500}'

# Verify Cross-Chain Bridge Memo
npx -y agentcash@latest fetch \
  -X POST https://gateway.stealthmark.io/x402/verify-crosschain-bridge-memo \
  -H "Content-Type: application/json" \
  -d '{"memo": "SWAP:ETH.ETH:0x0000000000000000000000000000000000000001:100000:omni:10"}'
```
