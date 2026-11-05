"""Configuration and protocol fee parameters for Omni Execution MCP.

Strictly non-custodial and secret-free: only reads public builder / referral
addresses, integrator IDs, and protocol attribution credentials from environment variables.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping, Optional

# Protocol & Safety Fee Ceilings
MAX_HYPERLIQUID_TENTHS_BPS = 1000  # Spot max is 1000 (1.00%), Perps cap is 100 (0.10%)
MAX_HYPERLIQUID_PERP_TENTHS_BPS = 100  # 10 bps (0.10%)
MAX_STANDARD_FEE_BPS = 100  # 100 bps (1.00%)

# Default Public Non-Custodial Integrator Fee Wallets
DEFAULT_EVM_FEE_WALLET = "0xc8dc4ecb93c4c665fad07e1261f5f7d60e3c6e0f"
DEFAULT_SOLANA_FEE_WALLET = "49sNhYNVSFxBuMPh5S2EC6Wkyay7mBWEPVv7pcyHJDxu"
DISABLED_WALLET_VALUES = {"", "none", "disabled", "off"}

EVM_ADDRESS_REGEX = re.compile(r"^0x[0-9a-fA-F]{40}$")
SOLANA_PUBKEY_REGEX = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _validate_evm_address(name: str, value: Optional[str]) -> Optional[str]:
    if not value or not value.strip():
        return None
    val = value.strip()
    if not EVM_ADDRESS_REGEX.match(val):
        raise ValueError(
            f"Invalid EVM address for {name}: '{val}'. Must be 0x followed by 40 hex characters."
        )
    return val.lower()


def _validate_solana_pubkey(name: str, value: Optional[str]) -> Optional[str]:
    if not value or not value.strip():
        return None
    val = value.strip()
    if not SOLANA_PUBKEY_REGEX.match(val):
        raise ValueError(
            f"Invalid Solana public key for {name}: '{val}'. Must be 32-44 base58 characters."
        )
    return val


def _resolve_wallet_from_env(
    name: str,
    raw_val: Optional[str],
    default_wallet: Optional[str],
    validator: Any,
) -> Optional[str]:
    """Resolves wallet address with Day-0 monetization defaults and explicit opt-out handling."""
    if raw_val is None:
        return default_wallet
    stripped = str(raw_val).strip()
    if stripped.lower() in DISABLED_WALLET_VALUES:
        return None
    return validator(name, stripped)


def _parse_fee_int(
    name: str,
    val_str: Optional[str],
    default: int,
    max_cap: int,
) -> int:
    if val_str is None or val_str.strip() == "":
        return default
    try:
        val = int(val_str.strip())
    except ValueError:
        raise ValueError(f"Environment variable {name} must be an integer, got: '{val_str}'")

    if val < 0:
        raise ValueError(f"Fee {name} cannot be negative: {val}")
    if val > max_cap:
        raise ValueError(
            f"Configured fee {name}={val} exceeds safety ceiling of {max_cap} bps/tenths_bps."
        )
    return val


@dataclass(frozen=True)
class OmniFeeConfig:
    """Immutable configuration container for integrator fee rails."""

    # Rail 1: Hyperliquid Builder Code
    hyperliquid_builder_address: Optional[str] = DEFAULT_EVM_FEE_WALLET
    hyperliquid_builder_tenths_bps: int = 100  # 10 bps (0.10% perp max)

    # Rail 2: Jupiter v6 Referral (Solana)
    jupiter_referral_account: Optional[str] = DEFAULT_SOLANA_FEE_WALLET
    jupiter_fee_bps: int = 35  # 0.35%

    # Rail 3: LI.FI Cross-Chain FeeForwarder
    lifi_integrator_id: str = "omni-execution-mcp"
    lifi_fee_bps: int = 35  # 0.35%

    # Rail 4: CoW Protocol MEV-Safe Partner Fee
    cow_partner_fee_recipient: Optional[str] = DEFAULT_EVM_FEE_WALLET
    cow_partner_fee_bps: int = 35  # 0.35%

    # Rail 5: Polymarket Builder Program
    poly_builder_api_key: Optional[str] = None
    poly_builder_secret: Optional[str] = None
    poly_builder_passphrase: Optional[str] = None

    # Rail 6: 0x Swap API v2
    zerox_fee_recipient: Optional[str] = DEFAULT_EVM_FEE_WALLET
    zerox_fee_bps: int = 35  # 0.35%

    @property
    def is_hyperliquid_active(self) -> bool:
        return bool(self.hyperliquid_builder_address)

    @property
    def is_jupiter_active(self) -> bool:
        return bool(self.jupiter_referral_account)

    @property
    def is_lifi_active(self) -> bool:
        return bool(self.lifi_integrator_id)

    @property
    def is_cow_active(self) -> bool:
        return bool(self.cow_partner_fee_recipient)

    @property
    def is_polymarket_active(self) -> bool:
        return bool(
            self.poly_builder_api_key
            and self.poly_builder_secret
            and self.poly_builder_passphrase
        )

    @property
    def is_zerox_active(self) -> bool:
        return bool(self.zerox_fee_recipient)

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> OmniFeeConfig:
        """Loads and validates fee configuration from environment variables or dictionary."""
        source = os.environ if env is None else env

        # Rail 1 - Hyperliquid
        hl_addr = _resolve_wallet_from_env(
            "HYPERLIQUID_BUILDER_ADDRESS",
            source.get("HYPERLIQUID_BUILDER_ADDRESS"),
            DEFAULT_EVM_FEE_WALLET,
            _validate_evm_address,
        )
        hl_tenths = _parse_fee_int(
            "HYPERLIQUID_BUILDER_TENTHS_BPS",
            source.get("HYPERLIQUID_BUILDER_TENTHS_BPS"),
            default=100 if hl_addr else 0,
            max_cap=MAX_HYPERLIQUID_TENTHS_BPS,
        )
        if hl_addr is None:
            hl_tenths = 0

        # Rail 2 - Jupiter
        jup_acc = _resolve_wallet_from_env(
            "JUPITER_REFERRAL_ACCOUNT",
            source.get("JUPITER_REFERRAL_ACCOUNT"),
            DEFAULT_SOLANA_FEE_WALLET,
            _validate_solana_pubkey,
        )
        jup_fee = _parse_fee_int(
            "JUPITER_FEE_BPS",
            source.get("JUPITER_FEE_BPS"),
            default=35,
            max_cap=MAX_STANDARD_FEE_BPS,
        )

        # Rail 3 - LI.FI
        lifi_id = (source.get("LIFI_INTEGRATOR_ID") or "omni-execution-mcp").strip()
        lifi_fee = _parse_fee_int(
            "LIFI_FEE_BPS",
            source.get("LIFI_FEE_BPS"),
            default=35,
            max_cap=MAX_STANDARD_FEE_BPS,
        )

        # Rail 4 - CoW Protocol
        cow_recipient = _resolve_wallet_from_env(
            "COW_PARTNER_FEE_RECIPIENT",
            source.get("COW_PARTNER_FEE_RECIPIENT"),
            DEFAULT_EVM_FEE_WALLET,
            _validate_evm_address,
        )
        cow_fee = _parse_fee_int(
            "COW_PARTNER_FEE_BPS",
            source.get("COW_PARTNER_FEE_BPS"),
            default=35,
            max_cap=MAX_STANDARD_FEE_BPS,
        )

        # Rail 5 - Polymarket
        poly_key = source.get("POLY_BUILDER_API_KEY")
        poly_secret = source.get("POLY_BUILDER_SECRET")
        poly_passphrase = source.get("POLY_BUILDER_PASSPHRASE")

        # Rail 6 - 0x
        zerox_recipient = _resolve_wallet_from_env(
            "ZEROX_FEE_RECIPIENT",
            source.get("ZEROX_FEE_RECIPIENT"),
            DEFAULT_EVM_FEE_WALLET,
            _validate_evm_address,
        )
        zerox_fee = _parse_fee_int(
            "ZEROX_FEE_BPS",
            source.get("ZEROX_FEE_BPS"),
            default=35,
            max_cap=MAX_STANDARD_FEE_BPS,
        )

        return cls(
            hyperliquid_builder_address=hl_addr,
            hyperliquid_builder_tenths_bps=hl_tenths,
            jupiter_referral_account=jup_acc,
            jupiter_fee_bps=jup_fee,
            lifi_integrator_id=lifi_id,
            lifi_fee_bps=lifi_fee,
            cow_partner_fee_recipient=cow_recipient,
            cow_partner_fee_bps=cow_fee,
            poly_builder_api_key=poly_key.strip() if poly_key else None,
            poly_builder_secret=poly_secret.strip() if poly_secret else None,
            poly_builder_passphrase=poly_passphrase.strip() if poly_passphrase else None,
            zerox_fee_recipient=zerox_recipient,
            zerox_fee_bps=zerox_fee,
        )
