"""Pure Python stdlib Solana SPL & Token-2022 rent reclaimer and dust analyzer.

Mine H1: Non-custodial scanner and transaction builder for reclaiming Solana rent
from empty ATAs, solving the Token-2022 withheld transfer fee closure blockage,
and generating batched unsigned instruction descriptors with transparent 5% fee math.
"""

from __future__ import annotations

import base64
import json
import struct
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Union

from .config import DEFAULT_SOLANA_FEE_WALLET

# Official Solana Program IDs
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
SOL_MINT = "So11111111111111111111111111111111111111112"

# Base58 Alphabet (Bitcoin / Solana)
B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s: str) -> bytes:
    """Decodes a Base58-encoded string into bytes."""
    if not isinstance(s, str) or not s:
        raise ValueError("Invalid Base58 string: input must be a non-empty string")
    num = 0
    for char in s:
        idx = B58_ALPHABET.find(char)
        if idx == -1:
            raise ValueError(f"Invalid Base58 character: '{char}'")
        num = num * 58 + idx

    full_bytes: List[int] = []
    while num > 0:
        full_bytes.append(num % 256)
        num //= 256
    full_bytes_b = bytes(reversed(full_bytes))

    pad = 0
    for char in s:
        if char == "1":
            pad += 1
        else:
            break
    return (b"\x00" * pad) + full_bytes_b


def b58encode(b: bytes) -> str:
    """Encodes bytes into a Base58 string."""
    if not isinstance(b, (bytes, bytearray)):
        raise TypeError("Expected bytes or bytearray for Base58 encoding")
    if len(b) == 0:
        return ""
    pad = 0
    for byte in b:
        if byte == 0:
            pad += 1
        else:
            break
    num = int.from_bytes(b, byteorder="big")
    res: List[str] = []
    while num > 0:
        res.append(B58_ALPHABET[num % 58])
        num //= 58
    return ("1" * pad) + "".join(reversed(res))


def validate_solana_pubkey(name: str, pubkey: str) -> str:
    """Validates that pubkey is a valid 32-byte Base58 Solana public key."""
    if not pubkey or not isinstance(pubkey, str):
        raise ValueError(f"{name} must be a non-empty string")
    pk = pubkey.strip()
    try:
        decoded = b58decode(pk)
    except Exception as exc:
        raise ValueError(f"Invalid Solana public key for {name}: '{pk}' ({exc})") from exc
    if len(decoded) != 32:
        raise ValueError(
            f"Invalid Solana public key for {name}: '{pk}'. Must decode to exactly 32 bytes, got {len(decoded)}"
        )
    return pk


def _rpc_request(
    rpc_url: str,
    method: str,
    params: list,
    transport: Optional[Any] = None,
) -> Dict[str, Any]:
    """Sends a Solana JSON-RPC 2.0 request via transport mock or standard HTTP."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }

    if transport is not None:
        if callable(transport):
            import inspect

            sig = inspect.signature(transport)
            if len(sig.parameters) >= 2:
                return transport(rpc_url, payload)
            return transport(payload)
        elif hasattr(transport, "post"):
            return transport.post(rpc_url, payload)
        elif hasattr(transport, "call"):
            return transport.call(method, params)
        elif isinstance(transport, dict):
            # Check for programId in params
            prog_id = None
            if len(params) > 1 and isinstance(params[1], dict):
                prog_id = params[1].get("programId")
            if prog_id and prog_id in transport:
                return transport[prog_id]
            if method in transport:
                return transport[method]
            return transport

    req = urllib.request.Request(
        rpc_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "omni-execution-mcp/1.1.0"},
    )
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def scan_solana_reclaimable_rent(
    owner_pubkey: str,
    rpc_url: str = "https://api.mainnet-beta.solana.com",
    rpc_transport: Optional[Any] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Scans a Solana account for empty SPL and Token-2022 token accounts,

    detects Token-2022 withheld transfer fees, identifies swappable dust,
    and calculates exact reclaimable lamports and 5% service fee math.
    """
    transport = rpc_transport if rpc_transport is not None else kwargs.get("rpc_Transport")
    owner = validate_solana_pubkey("owner_pubkey", owner_pubkey)

    programs = [
        ("SPL Token", TOKEN_PROGRAM_ID),
        ("Token-2022", TOKEN_2022_PROGRAM_ID),
    ]

    empty_closable: List[Dict[str, Any]] = []
    token2022_withheld_blocked: List[Dict[str, Any]] = []
    dust_swappable: List[Dict[str, Any]] = []
    skipped_frozen_or_nonclosable: List[Dict[str, Any]] = []

    for prog_name, prog_id in programs:
        params = [
            owner,
            {"programId": prog_id},
            {"encoding": "jsonParsed", "commitment": "confirmed"},
        ]
        try:
            resp = _rpc_request(rpc_url, "getTokenAccountsByOwner", params, transport)
        except Exception as exc:
            # If RPC call fails, record empty for this program
            resp = {"result": {"value": []}, "error": str(exc)}

        accounts: List[Dict[str, Any]] = []
        if isinstance(resp, dict):
            result = resp.get("result")
            if isinstance(result, dict):
                accounts = result.get("value", [])
            elif isinstance(result, list):
                accounts = result
        elif isinstance(resp, list):
            accounts = resp

        for item in accounts:
            acc_pubkey = item.get("pubkey", "")
            acc_data = item.get("account", {})
            lamports = int(acc_data.get("lamports", 0))

            parsed_data = acc_data.get("data", {})
            parsed_info = (
                parsed_data.get("parsed", {}).get("info", {})
                if isinstance(parsed_data, dict)
                else {}
            )

            mint = parsed_info.get("mint", "")
            state = str(parsed_info.get("state", "initialized")).lower()
            token_amount = parsed_info.get("tokenAmount", {})
            raw_amount = str(token_amount.get("amount", "0"))
            decimals = int(token_amount.get("decimals", 0))
            ui_amount = token_amount.get("uiAmount")
            extensions = parsed_info.get("extensions", [])

            # Extension inspection
            withheld_amount = "0"
            is_non_transferable = False
            if isinstance(extensions, list):
                for ext in extensions:
                    if isinstance(ext, dict):
                        ext_type = ext.get("extension")
                        if ext_type == "transferFeeAmount":
                            withheld_amount = str(
                                ext.get("state", {}).get("withheldAmount", "0")
                            )
                        elif ext_type == "nonTransferable":
                            is_non_transferable = True

            # Classification
            if state == "frozen":
                skipped_frozen_or_nonclosable.append(
                    {
                        "account_pubkey": acc_pubkey,
                        "pubkey": acc_pubkey,
                        "mint": mint,
                        "program_id": prog_id,
                        "program_name": prog_name,
                        "lamports": lamports,
                        "amount": raw_amount,
                        "reason": "Account is frozen by freeze authority",
                    }
                )
            elif is_non_transferable and raw_amount != "0":
                skipped_frozen_or_nonclosable.append(
                    {
                        "account_pubkey": acc_pubkey,
                        "pubkey": acc_pubkey,
                        "mint": mint,
                        "program_id": prog_id,
                        "program_name": prog_name,
                        "lamports": lamports,
                        "amount": raw_amount,
                        "reason": "Non-transferable soulbound account with active balance",
                    }
                )
            elif raw_amount == "0":
                # Check for Token-2022 withheld transfer fee blockage
                if (
                    prog_id == TOKEN_2022_PROGRAM_ID
                    and withheld_amount != "0"
                    and int(withheld_amount) > 0
                ):
                    token2022_withheld_blocked.append(
                        {
                            "account_pubkey": acc_pubkey,
                            "pubkey": acc_pubkey,
                            "mint": mint,
                            "program_id": prog_id,
                            "program_name": prog_name,
                            "lamports": lamports,
                            "withheld_amount": withheld_amount,
                            "reclaimable_sol": round(lamports / 1e9, 9),
                            "status": "requires_harvest_before_close",
                        }
                    )
                else:
                    empty_closable.append(
                        {
                            "account_pubkey": acc_pubkey,
                            "pubkey": acc_pubkey,
                            "mint": mint,
                            "program_id": prog_id,
                            "program_name": prog_name,
                            "lamports": lamports,
                            "reclaimable_sol": round(lamports / 1e9, 9),
                            "status": "immediately_closable",
                        }
                    )
            else:
                # Dust swappable via Jupiter quote
                jup_params = {
                    "inputMint": mint,
                    "outputMint": SOL_MINT,
                    "amount": raw_amount,
                    "slippageBps": "50",
                    "platformFeeBps": "35",
                    "feeAccount": DEFAULT_SOLANA_FEE_WALLET,
                }
                jup_url = (
                    f"https://quote-api.jup.ag/v6/quote?{urllib.parse.urlencode(jup_params)}"
                )
                dust_swappable.append(
                    {
                        "account_pubkey": acc_pubkey,
                        "pubkey": acc_pubkey,
                        "mint": mint,
                        "program_id": prog_id,
                        "program_name": prog_name,
                        "amount": raw_amount,
                        "decimals": decimals,
                        "ui_amount": ui_amount,
                        "lamports": lamports,
                        "jupiter_quote_params": jup_params,
                        "jupiter_quote_url": jup_url,
                    }
                )

    total_reclaimable_lamports = sum(a["lamports"] for a in empty_closable) + sum(
        a["lamports"] for a in token2022_withheld_blocked
    )
    service_fee_lamports = int(total_reclaimable_lamports * 500 // 10000)  # 5% (500 bps)
    net_user_lamports = total_reclaimable_lamports - service_fee_lamports

    return {
        "owner_pubkey": owner,
        "fee_recipient": DEFAULT_SOLANA_FEE_WALLET,
        "fee_bps": 500,
        "fee_percentage": "5.0%",
        "total_accounts_scanned": (
            len(empty_closable)
            + len(token2022_withheld_blocked)
            + len(dust_swappable)
            + len(skipped_frozen_or_nonclosable)
        ),
        "total_closable_accounts": len(empty_closable) + len(token2022_withheld_blocked),
        "empty_closable": empty_closable,
        "token2022_withheld_blocked": token2022_withheld_blocked,
        "dust_swappable": dust_swappable,
        "skipped_frozen_or_nonclosable": skipped_frozen_or_nonclosable,
        "total_reclaimable_lamports": total_reclaimable_lamports,
        "service_fee_lamports": service_fee_lamports,
        "net_user_lamports": net_user_lamports,
        "reclaimable_sol_estimate": round(total_reclaimable_lamports / 1e9, 9),
        "service_fee_sol_estimate": round(service_fee_lamports / 1e9, 9),
        "net_user_sol_estimate": round(net_user_lamports / 1e9, 9),
    }


def build_solana_rent_reclaim_batches(
    scan_result: Dict[str, Any],
    max_accounts_per_tx: int = 20,
    fee_bps: int = 500,
    fee_recipient: str = DEFAULT_SOLANA_FEE_WALLET,
) -> Dict[str, Any]:
    """Groups reclaimable accounts into deterministic transaction batches of <= 20 accounts.

    For each batch, outputs instructions:
    1. Token-2022 HarvestWithheldTokensToMint [26, 4] for withheld-blocked accounts.
    2. CloseAccount [9] for each account.
    3. SystemProgram.transfer [2, batch_fee_lamports] only if batch_fee_lamports > 0.
    """
    owner_raw = scan_result.get("owner_pubkey", "")
    owner_pubkey = validate_solana_pubkey("owner_pubkey", owner_raw)
    fee_rec = validate_solana_pubkey(
        "fee_recipient", fee_recipient or DEFAULT_SOLANA_FEE_WALLET
    )

    if max_accounts_per_tx <= 0:
        max_accounts_per_tx = 20

    # Combine all closable accounts (prioritizing withheld accounts first for deterministic ordering)
    closable_accounts = list(scan_result.get("token2022_withheld_blocked", [])) + list(
        scan_result.get("empty_closable", [])
    )
    total_accounts_count = len(closable_accounts)

    batches: List[Dict[str, Any]] = []
    chunk_count = (
        (total_accounts_count + max_accounts_per_tx - 1) // max_accounts_per_tx
        if total_accounts_count > 0
        else 0
    )

    for i in range(chunk_count):
        batch_accs = closable_accounts[
            i * max_accounts_per_tx : (i + 1) * max_accounts_per_tx
        ]
        batch_reclaimable = sum(int(a.get("lamports", 0)) for a in batch_accs)
        batch_fee = int(batch_reclaimable * fee_bps // 10000)
        batch_net = batch_reclaimable - batch_fee

        instructions: List[Dict[str, Any]] = []

        for acc in batch_accs:
            acc_pk = acc.get("account_pubkey") or acc.get("pubkey")
            mint_pk = acc.get("mint")
            prog_id = acc.get("program_id") or TOKEN_PROGRAM_ID

            is_withheld_blocked = (
                acc.get("status") == "requires_harvest_before_close"
                or int(acc.get("withheld_amount", "0")) > 0
            )

            # Step 1: Token-2022 HarvestWithheldTokensToMint [26, 4]
            if is_withheld_blocked and prog_id == TOKEN_2022_PROGRAM_ID and mint_pk:
                harvest_data = bytes([26, 4])
                instructions.append(
                    {
                        "instruction_type": "Token2022_HarvestWithheldTokensToMint",
                        "programId": TOKEN_2022_PROGRAM_ID,
                        "keys": [
                            {"pubkey": mint_pk, "isSigner": False, "isWritable": True},
                            {"pubkey": acc_pk, "isSigner": False, "isWritable": True},
                        ],
                        "data": base64.b64encode(harvest_data).decode("utf-8"),
                        "data_hex": harvest_data.hex(),
                        "description": f"Harvest withheld transfer fees from token account {acc_pk} to mint {mint_pk}",
                    }
                )

            # Step 2: CloseAccount instruction [9]
            close_data = bytes([9])
            instructions.append(
                {
                    "instruction_type": "CloseAccount",
                    "programId": prog_id,
                    "keys": [
                        {"pubkey": acc_pk, "isSigner": False, "isWritable": True},
                        {"pubkey": owner_pubkey, "isSigner": False, "isWritable": True},
                        {"pubkey": owner_pubkey, "isSigner": True, "isWritable": False},
                    ],
                    "data": base64.b64encode(close_data).decode("utf-8"),
                    "data_hex": close_data.hex(),
                    "description": f"Close account {acc_pk} and reclaim {acc.get('lamports', 0)} lamports to {owner_pubkey}",
                }
            )

        # Step 3: SystemProgram.transfer [2, batch_fee_lamports] only if batch_fee > 0
        if batch_fee > 0:
            transfer_data = struct.pack("<IQ", 2, batch_fee)
            instructions.append(
                {
                    "instruction_type": "SystemProgram_Transfer",
                    "programId": SYSTEM_PROGRAM_ID,
                    "keys": [
                        {"pubkey": owner_pubkey, "isSigner": True, "isWritable": True},
                        {"pubkey": fee_rec, "isSigner": False, "isWritable": True},
                    ],
                    "data": base64.b64encode(transfer_data).decode("utf-8"),
                    "data_hex": transfer_data.hex(),
                    "description": f"Transfer service fee of {batch_fee} lamports ({fee_bps / 100:.2f}%) to {fee_rec}",
                }
            )

        batches.append(
            {
                "batch_index": i,
                "accounts_count": len(batch_accs),
                "reclaimable_lamports": batch_reclaimable,
                "service_fee_lamports": batch_fee,
                "net_user_lamports": batch_net,
                "reclaimable_sol": round(batch_reclaimable / 1e9, 9),
                "service_fee_sol": round(batch_fee / 1e9, 9),
                "net_user_sol": round(batch_net / 1e9, 9),
                "instructions_count": len(instructions),
                "instructions": instructions,
            }
        )

    total_reclaimed = sum(b["reclaimable_lamports"] for b in batches)
    total_fee = sum(b["service_fee_lamports"] for b in batches)
    total_net = sum(b["net_user_lamports"] for b in batches)

    return {
        "owner_pubkey": owner_pubkey,
        "fee_recipient": fee_rec,
        "fee_bps": fee_bps,
        "fee_percentage": f"{fee_bps / 100:.2f}%",
        "total_accounts_reclaimed": total_accounts_count,
        "total_batches": len(batches),
        "total_reclaimable_lamports": total_reclaimed,
        "total_service_fee_lamports": total_fee,
        "total_net_user_lamports": total_net,
        "reclaimable_sol_estimate": round(total_reclaimed / 1e9, 9),
        "service_fee_sol_estimate": round(total_fee / 1e9, 9),
        "net_user_sol_estimate": round(total_net / 1e9, 9),
        "batches": batches,
        "non_custodial_notice": (
            "100% Non-Custodial: All instructions are unsigned. Reclaimed rent is deposited "
            "directly to the owner wallet, and service fee is transferred in the same transaction."
        ),
    }
