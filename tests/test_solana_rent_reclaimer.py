"""Unit tests for Solana SPL & Token-2022 Rent Reclaimer and MCP tools.

Tests cover:
- Base58 encode/decode and 32-byte pubkey validation.
- Empty SPL ATA detection and lamports accounting.
- Token-2022 withheld transfer fee detection and blockage isolation.
- HarvestWithheldTokensToMint [26, 4] + CloseAccount [9] instruction ordering.
- Frozen and non-transferable account skipping.
- Swappable dust identification with Jupiter quote parameters (35 bps platform fee).
- 5% (500 bps) SystemProgram.transfer fee math and zero-fee exemption.
- Batching into <= 20 accounts per transaction.
- MCP tools/list and tools/call integration with mocked RPC transport.
"""

from __future__ import annotations

import base64
import json
import struct
import unittest
from unittest.mock import MagicMock, patch

from omni_execution_mcp.config import DEFAULT_SOLANA_FEE_WALLET, OmniFeeConfig
from omni_execution_mcp.server import handle_jsonrpc_message
from omni_execution_mcp.solana_rent_reclaimer import (
    SOL_MINT,
    SYSTEM_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    b58decode,
    b58encode,
    build_solana_rent_reclaim_batches,
    scan_solana_reclaimable_rent,
    validate_solana_pubkey,
)

# Test Solana Public Keys (Valid 32-byte Base58)
TEST_OWNER = "49sNhYNVSFxBuMPh5S2EC6Wkyay7mBWEPVv7pcyHJDxu"
TEST_MINT_SPL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TEST_MINT_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TEST_ATA_EMPTY_SPL = "3pM5Y3vQ5gG9N2F1K8j9d6P4m3a2b1c0D9e8F7a6B5c4"
TEST_ATA_WITHHELD_2022 = "7a6B5c4D9e8F7a6B5c43pM5Y3vQ5gG9N2F1K8j9d6P4m"
TEST_ATA_EMPTY_2022 = "5c43pM5Y3vQ5gG9N2F1K8j9d6P4m7a6B5c4D9e8F7a6B"
TEST_ATA_FROZEN = "9d6P4m7a6B5c4D9e8F7a6B5c43pM5Y3vQ5gG9N2F1K8j"
TEST_ATA_DUST = "8j9d6P4m3a2b1c0D9e8F7a6B5c43pM5Y3vQ5gG9N2F1K"


def _create_mock_account(
    pubkey: str,
    mint: str,
    program_id: str,
    amount: str = "0",
    lamports: int = 2039280,
    state: str = "initialized",
    withheld_amount: str = "0",
    non_transferable: bool = False,
    decimals: int = 6,
) -> dict:
    extensions = []
    if withheld_amount != "0":
        extensions.append(
            {
                "extension": "transferFeeAmount",
                "state": {"withheldAmount": withheld_amount},
            }
        )
    if non_transferable:
        extensions.append({"extension": "nonTransferable"})

    return {
        "pubkey": pubkey,
        "account": {
            "lamports": lamports,
            "data": {
                "program": "spl-token-2022" if program_id == TOKEN_2022_PROGRAM_ID else "spl-token",
                "parsed": {
                    "info": {
                        "mint": mint,
                        "owner": TEST_OWNER,
                        "state": state,
                        "tokenAmount": {
                            "amount": amount,
                            "decimals": decimals,
                            "uiAmount": float(amount) / (10**decimals) if decimals else float(amount),
                            "uiAmountString": amount,
                        },
                        "extensions": extensions,
                    },
                    "type": "account",
                },
            },
        },
    }


class TestSolanaRentReclaimer(unittest.TestCase):
    def setUp(self):
        self.spl_empty = _create_mock_account(
            pubkey=TEST_ATA_EMPTY_SPL,
            mint=TEST_MINT_SPL,
            program_id=TOKEN_PROGRAM_ID,
            amount="0",
            lamports=2039280,
        )
        self.token2022_withheld = _create_mock_account(
            pubkey=TEST_ATA_WITHHELD_2022,
            mint=TEST_MINT_2022,
            program_id=TOKEN_2022_PROGRAM_ID,
            amount="0",
            lamports=2200000,
            withheld_amount="5000",
        )
        self.token2022_empty = _create_mock_account(
            pubkey=TEST_ATA_EMPTY_2022,
            mint=TEST_MINT_2022,
            program_id=TOKEN_2022_PROGRAM_ID,
            amount="0",
            lamports=2100000,
            withheld_amount="0",
        )
        self.frozen_account = _create_mock_account(
            pubkey=TEST_ATA_FROZEN,
            mint=TEST_MINT_SPL,
            program_id=TOKEN_PROGRAM_ID,
            amount="0",
            state="frozen",
            lamports=2039280,
        )
        self.dust_account = _create_mock_account(
            pubkey=TEST_ATA_DUST,
            mint=TEST_MINT_SPL,
            program_id=TOKEN_PROGRAM_ID,
            amount="1500000",
            lamports=2039280,
            decimals=6,
        )

    # --------------------------------------------------------------------------
    # 1. Base58 Encode / Decode Tests
    # --------------------------------------------------------------------------
    def test_b58_encode_decode_roundtrip(self):
        keys = [
            TEST_OWNER,
            DEFAULT_SOLANA_FEE_WALLET,
            TOKEN_PROGRAM_ID,
            TOKEN_2022_PROGRAM_ID,
            SYSTEM_PROGRAM_ID,
            SOL_MINT,
        ]
        for key in keys:
            raw_bytes = b58decode(key)
            self.assertEqual(len(raw_bytes), 32, f"Key {key} must decode to 32 bytes")
            re_encoded = b58encode(raw_bytes)
            self.assertEqual(re_encoded, key, f"Roundtrip failed for {key}")

    def test_b58_validation_errors(self):
        # Invalid Base58 character ('0', 'O', 'I', 'l')
        with self.assertRaises(ValueError):
            b58decode("0xInvalidBase58")

        # Empty string
        with self.assertRaises(ValueError):
            validate_solana_pubkey("test", "")

        # Wrong length (e.g. 16 bytes instead of 32)
        short_b58 = b58encode(b"\x01" * 16)
        with self.assertRaises(ValueError) as ctx:
            validate_solana_pubkey("test", short_b58)
        self.assertIn("Must decode to exactly 32 bytes", str(ctx.exception))

    # --------------------------------------------------------------------------
    # 2. Scanner Classification & Accounting Tests
    # --------------------------------------------------------------------------
    def test_scan_classification(self):
        mock_transport = {
            TOKEN_PROGRAM_ID: {
                "result": {"value": [self.spl_empty, self.frozen_account, self.dust_account]}
            },
            TOKEN_2022_PROGRAM_ID: {
                "result": {"value": [self.token2022_withheld, self.token2022_empty]}
            },
        }

        res = scan_solana_reclaimable_rent(
            owner_pubkey=TEST_OWNER,
            rpc_transport=mock_transport,
        )

        self.assertEqual(res["owner_pubkey"], TEST_OWNER)
        self.assertEqual(res["fee_recipient"], DEFAULT_SOLANA_FEE_WALLET)
        self.assertEqual(res["fee_bps"], 500)

        # 2 empty closable accounts: spl_empty + token2022_empty
        self.assertEqual(len(res["empty_closable"]), 2)
        closable_pks = {a["pubkey"] for a in res["empty_closable"]}
        self.assertIn(TEST_ATA_EMPTY_SPL, closable_pks)
        self.assertIn(TEST_ATA_EMPTY_2022, closable_pks)

        # 1 Token-2022 withheld blocked account
        self.assertEqual(len(res["token2022_withheld_blocked"]), 1)
        self.assertEqual(res["token2022_withheld_blocked"][0]["pubkey"], TEST_ATA_WITHHELD_2022)
        self.assertEqual(res["token2022_withheld_blocked"][0]["withheld_amount"], "5000")
        self.assertEqual(
            res["token2022_withheld_blocked"][0]["status"], "requires_harvest_before_close"
        )

        # 1 Frozen account skipped
        self.assertEqual(len(res["skipped_frozen_or_nonclosable"]), 1)
        self.assertEqual(res["skipped_frozen_or_nonclosable"][0]["pubkey"], TEST_ATA_FROZEN)

        # 1 Dust account with Jupiter quote parameters
        self.assertEqual(len(res["dust_swappable"]), 1)
        dust = res["dust_swappable"][0]
        self.assertEqual(dust["pubkey"], TEST_ATA_DUST)
        self.assertEqual(dust["amount"], "1500000")
        self.assertEqual(dust["jupiter_quote_params"]["platformFeeBps"], "35")
        self.assertEqual(dust["jupiter_quote_params"]["feeAccount"], DEFAULT_SOLANA_FEE_WALLET)

    def test_exact_5pct_fee_math(self):
        mock_transport = {
            TOKEN_PROGRAM_ID: {"result": {"value": [self.spl_empty]}},  # 2,039,280 lamports
            TOKEN_2022_PROGRAM_ID: {
                "result": {"value": [self.token2022_withheld, self.token2022_empty]}
            },  # 2,200,000 + 2,100,000 = 4,300,000 lamports
        }

        res = scan_solana_reclaimable_rent(
            owner_pubkey=TEST_OWNER,
            rpc_transport=mock_transport,
        )

        total_reclaimable = 2039280 + 2200000 + 2100000  # 6,339,280 lamports
        expected_fee = int(total_reclaimable * 500 // 10000)  # 316,964 lamports (exact 5%)
        expected_net = total_reclaimable - expected_fee  # 6,022,316 lamports (95%)

        self.assertEqual(res["total_reclaimable_lamports"], total_reclaimable)
        self.assertEqual(res["service_fee_lamports"], expected_fee)
        self.assertEqual(res["net_user_lamports"], expected_net)
        self.assertEqual(res["reclaimable_sol_estimate"], round(total_reclaimable / 1e9, 9))
        self.assertEqual(res["service_fee_sol_estimate"], round(expected_fee / 1e9, 9))
        self.assertEqual(res["net_user_sol_estimate"], round(expected_net / 1e9, 9))

    # --------------------------------------------------------------------------
    # 3. Batched Instruction Generation & Ordering Tests
    # --------------------------------------------------------------------------
    def test_instruction_generation_and_token2022_harvest_ordering(self):
        scan_data = {
            "owner_pubkey": TEST_OWNER,
            "empty_closable": [
                {
                    "pubkey": TEST_ATA_EMPTY_SPL,
                    "mint": TEST_MINT_SPL,
                    "program_id": TOKEN_PROGRAM_ID,
                    "lamports": 2039280,
                }
            ],
            "token2022_withheld_blocked": [
                {
                    "pubkey": TEST_ATA_WITHHELD_2022,
                    "mint": TEST_MINT_2022,
                    "program_id": TOKEN_2022_PROGRAM_ID,
                    "lamports": 2200000,
                    "withheld_amount": "5000",
                    "status": "requires_harvest_before_close",
                }
            ],
        }

        batches = build_solana_rent_reclaim_batches(
            scan_result=scan_data,
            max_accounts_per_tx=20,
            fee_bps=500,
            fee_recipient=DEFAULT_SOLANA_FEE_WALLET,
        )

        self.assertEqual(batches["total_accounts_reclaimed"], 2)
        self.assertEqual(batches["total_batches"], 1)

        batch = batches["batches"][0]
        instructions = batch["instructions"]

        # Order must be:
        # 1. HarvestWithheldTokensToMint [26, 4] for token2022_withheld
        # 2. CloseAccount [9] for token2022_withheld
        # 3. CloseAccount [9] for spl_empty
        # 4. SystemProgram.transfer [2, fee]
        self.assertEqual(len(instructions), 4)

        # 1. Verify Harvest instruction
        ix_harvest = instructions[0]
        self.assertEqual(ix_harvest["instruction_type"], "Token2022_HarvestWithheldTokensToMint")
        self.assertEqual(ix_harvest["programId"], TOKEN_2022_PROGRAM_ID)
        self.assertEqual(ix_harvest["data_hex"], "1a04")  # [26, 4]
        self.assertEqual(ix_harvest["data"], base64.b64encode(bytes([26, 4])).decode("utf-8"))
        self.assertEqual(ix_harvest["keys"][0]["pubkey"], TEST_MINT_2022)
        self.assertTrue(ix_harvest["keys"][0]["isWritable"])
        self.assertFalse(ix_harvest["keys"][0]["isSigner"])
        self.assertEqual(ix_harvest["keys"][1]["pubkey"], TEST_ATA_WITHHELD_2022)
        self.assertTrue(ix_harvest["keys"][1]["isWritable"])

        # 2. Verify Close instruction for Token-2022
        ix_close_2022 = instructions[1]
        self.assertEqual(ix_close_2022["instruction_type"], "CloseAccount")
        self.assertEqual(ix_close_2022["programId"], TOKEN_2022_PROGRAM_ID)
        self.assertEqual(ix_close_2022["data_hex"], "09")  # [9]
        self.assertEqual(ix_close_2022["keys"][0]["pubkey"], TEST_ATA_WITHHELD_2022)
        self.assertEqual(ix_close_2022["keys"][1]["pubkey"], TEST_OWNER)  # destination
        self.assertEqual(ix_close_2022["keys"][2]["pubkey"], TEST_OWNER)  # authority / signer
        self.assertTrue(ix_close_2022["keys"][2]["isSigner"])

        # 3. Verify Close instruction for standard SPL
        ix_close_spl = instructions[2]
        self.assertEqual(ix_close_spl["instruction_type"], "CloseAccount")
        self.assertEqual(ix_close_spl["programId"], TOKEN_PROGRAM_ID)
        self.assertEqual(ix_close_spl["data_hex"], "09")

        # 4. Verify SystemProgram Transfer instruction
        ix_fee = instructions[3]
        self.assertEqual(ix_fee["instruction_type"], "SystemProgram_Transfer")
        self.assertEqual(ix_fee["programId"], SYSTEM_PROGRAM_ID)
        raw_transfer_bytes = base64.b64decode(ix_fee["data"])
        self.assertEqual(len(raw_transfer_bytes), 12)
        ix_code, transfer_lamports = struct.unpack("<IQ", raw_transfer_bytes)
        self.assertEqual(ix_code, 2)  # SystemProgram Transfer opcode = 2
        expected_batch_fee = int((2039280 + 2200000) * 500 // 10000)
        self.assertEqual(transfer_lamports, expected_batch_fee)
        self.assertEqual(ix_fee["keys"][0]["pubkey"], TEST_OWNER)
        self.assertTrue(ix_fee["keys"][0]["isSigner"])
        self.assertEqual(ix_fee["keys"][1]["pubkey"], DEFAULT_SOLANA_FEE_WALLET)
        self.assertFalse(ix_fee["keys"][1]["isSigner"])

    def test_zero_fee_omits_transfer_instruction(self):
        scan_data = {
            "owner_pubkey": TEST_OWNER,
            "empty_closable": [
                {
                    "pubkey": TEST_ATA_EMPTY_SPL,
                    "mint": TEST_MINT_SPL,
                    "program_id": TOKEN_PROGRAM_ID,
                    "lamports": 2039280,
                }
            ],
            "token2022_withheld_blocked": [],
        }

        # fee_bps = 0
        batches = build_solana_rent_reclaim_batches(
            scan_result=scan_data,
            fee_bps=0,
        )
        instructions = batches["batches"][0]["instructions"]
        self.assertEqual(len(instructions), 1)
        self.assertEqual(instructions[0]["instruction_type"], "CloseAccount")
        # Ensure no SystemProgram_Transfer exists
        self.assertNotIn("SystemProgram_Transfer", [ix["instruction_type"] for ix in instructions])

    def test_batch_chunking_exceeding_20(self):
        # Create 45 dummy accounts
        accounts = [
            {
                "pubkey": f"acc_{i}_{TEST_ATA_EMPTY_SPL[:30]}",
                "mint": TEST_MINT_SPL,
                "program_id": TOKEN_PROGRAM_ID,
                "lamports": 2039280,
            }
            for i in range(45)
        ]
        scan_data = {
            "owner_pubkey": TEST_OWNER,
            "empty_closable": accounts,
            "token2022_withheld_blocked": [],
        }

        batches = build_solana_rent_reclaim_batches(
            scan_result=scan_data,
            max_accounts_per_tx=20,
        )
        self.assertEqual(batches["total_batches"], 3)
        self.assertEqual(batches["batches"][0]["accounts_count"], 20)
        self.assertEqual(batches["batches"][1]["accounts_count"], 20)
        self.assertEqual(batches["batches"][2]["accounts_count"], 5)

    # --------------------------------------------------------------------------
    # 4. MCP Tools Integration Tests
    # --------------------------------------------------------------------------
    @patch("omni_execution_mcp.server.scan_solana_reclaimable_rent")
    def test_mcp_tool_solana_scan_reclaimable_rent(self, mock_scan):
        mock_scan.return_value = {
            "owner_pubkey": TEST_OWNER,
            "total_reclaimable_lamports": 4078560,
            "empty_closable": [],
        }

        msg = {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "tools/call",
            "params": {
                "name": "solana_scan_reclaimable_rent",
                "arguments": {
                    "owner_pubkey": TEST_OWNER,
                },
            },
        }

        res = handle_jsonrpc_message(msg)
        self.assertIsNotNone(res)
        self.assertEqual(res["id"], 101)
        self.assertFalse(res["result"]["isError"])
        payload = json.loads(res["result"]["content"][0]["text"])
        self.assertEqual(payload["owner_pubkey"], TEST_OWNER)
        self.assertEqual(payload["total_reclaimable_lamports"], 4078560)
        mock_scan.assert_called_once_with(
            owner_pubkey=TEST_OWNER,
            rpc_url="https://api.mainnet-beta.solana.com",
        )

    @patch("omni_execution_mcp.server.scan_solana_reclaimable_rent")
    def test_mcp_tool_solana_build_rent_reclaim_batches(self, mock_scan):
        mock_scan.return_value = {
            "owner_pubkey": TEST_OWNER,
            "empty_closable": [
                {
                    "pubkey": TEST_ATA_EMPTY_SPL,
                    "mint": TEST_MINT_SPL,
                    "program_id": TOKEN_PROGRAM_ID,
                    "lamports": 2039280,
                }
            ],
            "token2022_withheld_blocked": [],
        }

        msg = {
            "jsonrpc": "2.0",
            "id": 102,
            "method": "tools/call",
            "params": {
                "name": "solana_build_rent_reclaim_batches",
                "arguments": {
                    "owner_pubkey": TEST_OWNER,
                    "fee_bps": 500,
                },
            },
        }

        res = handle_jsonrpc_message(msg)
        self.assertIsNotNone(res)
        self.assertEqual(res["id"], 102)
        self.assertFalse(res["result"]["isError"])
        payload = json.loads(res["result"]["content"][0]["text"])
        self.assertEqual(payload["owner_pubkey"], TEST_OWNER)
        self.assertEqual(payload["total_batches"], 1)
        self.assertEqual(payload["fee_bps"], 500)
        self.assertEqual(payload["fee_recipient"], DEFAULT_SOLANA_FEE_WALLET)

    def test_mcp_tool_error_boundary_invalid_pubkey(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 103,
            "method": "tools/call",
            "params": {
                "name": "solana_scan_reclaimable_rent",
                "arguments": {
                    "owner_pubkey": "not-a-valid-solana-key",
                },
            },
        }

        res = handle_jsonrpc_message(msg)
        self.assertIsNotNone(res)
        self.assertEqual(res["id"], 103)
        self.assertTrue(res["result"]["isError"])
        self.assertIn("Invalid Solana public key", res["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
