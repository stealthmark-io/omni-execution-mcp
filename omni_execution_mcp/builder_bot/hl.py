"""Hyperliquid helpers for the builder bot.

Order wires follow the official SDK (`order_request_to_order_wire`,
`order_wires_to_order_action`) key for key: the action is msgpack-hashed, so key
order, lowercase builder address and normalized number strings all affect the
signature. Signing is delegated to the SDK and imported lazily.
"""

from __future__ import annotations

import json
import secrets
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from ..config import MAX_HYPERLIQUID_PERP_TENTHS_BPS

API_URLS = {
    "mainnet": "https://api.hyperliquid.xyz",
    "testnet": "https://api.hyperliquid-testnet.xyz",
}
AGENT_NAME = "omnibot"
MARKET_SLIPPAGE = Decimal("0.01")
MIN_ORDER_USD = Decimal("10")
FILLS_PAGE_MIN = 500
USD6 = Decimal("0.000001")
WIRE_QUANT = Decimal("0.00000001")

Transport = Callable[[str, bytes], bytes]


def validate_perp_tenths(tenths_bps: int) -> int:
    if not 1 <= tenths_bps <= MAX_HYPERLIQUID_PERP_TENTHS_BPS:
        raise ValueError(
            f"Builder fee de perps fuera de rango: {tenths_bps} décimas de bp "
            f"(1 a {MAX_HYPERLIQUID_PERP_TENTHS_BPS}, o sea hasta 10 bps)."
        )
    return tenths_bps


def fee_rate_percent(tenths_bps: int) -> str:
    """Tenths of a basis point as HL's percent string: 10 -> "0.01%"."""
    validate_perp_tenths(tenths_bps)
    return f"{(Decimal(tenths_bps) / 1000).normalize():f}%"


def builder_fee_usd(notional_usd: Decimal, tenths_bps: int) -> Decimal:
    return (notional_usd * tenths_bps / Decimal(100000)).quantize(USD6, rounding=ROUND_HALF_EVEN)


def sign_page_url(
    base_url: str,
    network: str,
    account: str,
    builder: Optional[str] = None,
    tenths_bps: Optional[int] = None,
    agent: Optional[str] = None,
    agent_name: str = AGENT_NAME,
) -> str:
    """Link to firmar.html; params travel in the fragment, never to a server."""
    params = {"red": network, "cuenta": account.lower()}
    if builder:
        params["builder"] = builder.lower()
        params["tasa"] = fee_rate_percent(tenths_bps or 0)
    if agent:
        params["agente"] = agent.lower()
        params["nombre"] = agent_name
    return base_url + "#" + urllib.parse.urlencode(params)


def to_wire(value: Decimal) -> str:
    if value != value.quantize(WIRE_QUANT):
        raise ValueError(f"El número {value} tiene más de 8 decimales.")
    text = format(value.normalize(), "f")
    return "0" if text == "-0" else text


@dataclass(frozen=True)
class PerpAsset:
    index: int
    name: str
    sz_decimals: int


def resolve_perp(meta: Mapping[str, Any], coin: str) -> PerpAsset:
    wanted = coin.strip()
    listed = [
        (index, entry)
        for index, entry in enumerate(meta["universe"])
        if not entry.get("isDelisted")
    ]
    exact = [item for item in listed if item[1]["name"] == wanted]
    folded = [item for item in listed if item[1]["name"].lower() == wanted.lower()]
    for matches in (exact, folded):
        if len(matches) == 1:
            index, entry = matches[0]
            return PerpAsset(index, entry["name"], int(entry["szDecimals"]))
    raise ValueError(f"No encuentro el perp {coin} en Hyperliquid.")


def round_price(px: Decimal, sz_decimals: int) -> Decimal:
    """5 significant figures, then at most 6 - szDecimals decimals (perps)."""
    significant = px.quantize(Decimal(1).scaleb(px.adjusted() - 4), rounding=ROUND_HALF_EVEN)
    return significant.quantize(Decimal(1).scaleb(-(6 - sz_decimals)), rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class OrderPreview:
    coin: str
    is_buy: bool
    reduce_only: bool
    size: Decimal
    limit_px: Decimal
    mid: Decimal
    notional_usd: Decimal
    builder_fee_usd: Decimal
    tenths_bps: int
    action: Dict[str, Any]


def market_order(
    asset: PerpAsset,
    mid: Decimal,
    is_buy: bool,
    size: Decimal,
    reduce_only: bool,
    builder: str,
    tenths_bps: int,
) -> OrderPreview:
    """IOC limit order at mid +/- 1% carrying the builder code."""
    validate_perp_tenths(tenths_bps)
    factor = 1 + MARKET_SLIPPAGE if is_buy else 1 - MARKET_SLIPPAGE
    limit_px = round_price(mid * factor, asset.sz_decimals)
    wire = {
        "a": asset.index,
        "b": is_buy,
        "p": to_wire(limit_px),
        "s": to_wire(size),
        "r": reduce_only,
        "t": {"limit": {"tif": "Ioc"}},
    }
    action = {
        "type": "order",
        "orders": [wire],
        "grouping": "na",
        "builder": {"b": builder.lower(), "f": tenths_bps},
    }
    notional = (size * mid).quantize(USD6, rounding=ROUND_HALF_EVEN)
    return OrderPreview(
        coin=asset.name,
        is_buy=is_buy,
        reduce_only=reduce_only,
        size=size,
        limit_px=limit_px,
        mid=mid,
        notional_usd=notional,
        builder_fee_usd=builder_fee_usd(notional, tenths_bps),
        tenths_bps=tenths_bps,
        action=action,
    )


def _mid_for(mids: Mapping[str, str], asset: PerpAsset) -> Decimal:
    if asset.name not in mids:
        raise ValueError(f"Hyperliquid no informa precio para {asset.name}.")
    return Decimal(str(mids[asset.name]))


def open_order(
    meta: Mapping[str, Any],
    mids: Mapping[str, str],
    coin: str,
    is_buy: bool,
    usd: Decimal,
    builder: str,
    tenths_bps: int,
) -> OrderPreview:
    asset = resolve_perp(meta, coin)
    mid = _mid_for(mids, asset)
    if usd < MIN_ORDER_USD:
        raise ValueError("El mínimo por orden en Hyperliquid es 10 USD.")
    size = (usd / mid).quantize(Decimal(1).scaleb(-asset.sz_decimals), rounding=ROUND_DOWN)
    if size <= 0:
        raise ValueError("Con ese monto no llega al tamaño mínimo; subí el monto.")
    preview = market_order(asset, mid, is_buy, size, False, builder, tenths_bps)
    if size * preview.limit_px < MIN_ORDER_USD:
        raise ValueError("Con ese monto la orden queda bajo 10 USD; subí un poco el monto.")
    return preview


def close_order(
    meta: Mapping[str, Any],
    mids: Mapping[str, str],
    state: Mapping[str, Any],
    coin: str,
    builder: str,
    tenths_bps: int,
) -> OrderPreview:
    asset = resolve_perp(meta, coin)
    szi = Decimal(0)
    for item in state.get("assetPositions", []):
        position = item.get("position", {})
        if position.get("coin") == asset.name:
            szi = Decimal(str(position.get("szi", "0")))
    if szi == 0:
        raise ValueError(f"No tenés posición abierta en {asset.name}.")
    mid = _mid_for(mids, asset)
    return market_order(asset, mid, szi < 0, abs(szi), True, builder, tenths_bps)


def parse_order_response(resp: Any) -> Tuple[str, Optional[int], str]:
    """(estado, oid, detalle) with estado in filled | resting | error."""
    if not isinstance(resp, Mapping):
        return "error", None, str(resp)
    if resp.get("status") != "ok":
        return "error", None, str(resp.get("response", resp))
    statuses = resp.get("response", {}).get("data", {}).get("statuses", [])
    if not statuses:
        return "error", None, "Hyperliquid respondió sin estados."
    status = statuses[0]
    if "filled" in status:
        filled = status["filled"]
        return "filled", int(filled["oid"]), f"{filled['totalSz']} @ {filled['avgPx']}"
    if "resting" in status:
        return "resting", int(status["resting"]["oid"]), "quedó en el libro"
    return "error", None, str(status.get("error", status))


def exchange_payload(action: Mapping[str, Any], nonce: int, signature: Mapping[str, Any]) -> Dict[str, Any]:
    """Same envelope as the SDK's Exchange._post_action, without vault or expiry."""
    return {
        "action": action,
        "nonce": nonce,
        "signature": signature,
        "vaultAddress": None,
        "expiresAfter": None,
    }


def _urllib_transport(url: str, body: bytes) -> bytes:
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read()


class HyperliquidClient:
    def __init__(self, network: str, transport: Optional[Transport] = None) -> None:
        if network not in API_URLS:
            raise ValueError(f"Red desconocida: {network}")
        self.network = network
        self.base_url = API_URLS[network]
        self._transport = transport or _urllib_transport

    def _post(self, path: str, payload: Mapping[str, Any]) -> Any:
        raw = self._transport(self.base_url + path, json.dumps(payload).encode("utf-8"))
        return json.loads(raw)

    def info(self, payload: Mapping[str, Any]) -> Any:
        return self._post("/info", payload)

    def exchange(self, payload: Mapping[str, Any]) -> Any:
        return self._post("/exchange", payload)

    def meta(self) -> Dict[str, Any]:
        return self.info({"type": "meta"})

    def all_mids(self) -> Dict[str, str]:
        return self.info({"type": "allMids"})

    def clearinghouse_state(self, user: str) -> Dict[str, Any]:
        return self.info({"type": "clearinghouseState", "user": user})

    def max_builder_fee(self, user: str, builder: str) -> int:
        return int(self.info({"type": "maxBuilderFee", "user": user, "builder": builder}))

    def agent_owner(self, agent: str) -> Optional[str]:
        """Master account an agent wallet trades for, or None if not an agent."""
        role = self.info({"type": "userRole", "user": agent})
        if isinstance(role, Mapping) and role.get("role") == "agent":
            owner = role.get("data", {}).get("user")
            return owner.lower() if owner else None
        return None

    def fills_between(self, user: str, start_ms: int, end_ms: int) -> List[Dict[str, Any]]:
        """userFillsByTime paged by time; overlapping pages are deduped by tid in the ledger."""
        fills: List[Dict[str, Any]] = []
        start = start_ms
        while True:
            page = self.info(
                {"type": "userFillsByTime", "user": user, "startTime": start, "endTime": end_ms}
            )
            fills.extend(page)
            if len(page) < FILLS_PAGE_MIN:
                return fills
            newest = max(int(fill["time"]) for fill in page)
            if newest <= start:
                return fills
            start = newest


class AgentSigner:
    """Creates agent wallets and signs L1 actions through the official HL SDK.

    Agent keys exist only inside an eth_account V3 keystore encrypted with the
    operator passphrase. An agent can place and cancel orders but cannot
    withdraw or transfer: those are user-signed actions of the main wallet.
    """

    def __init__(self, passphrase: str, is_mainnet: bool) -> None:
        if not passphrase:
            raise ValueError("Falta BOT_KEYSTORE_PASSPHRASE para cifrar las agent wallets.")
        try:
            from eth_account import Account
            from hyperliquid.utils.signing import sign_l1_action
        except ImportError as exc:
            raise RuntimeError(
                "Falta el SDK de Hyperliquid: pip install 'omni-execution-mcp[bot]'"
            ) from exc
        self._account = Account
        self._sign_l1_action = sign_l1_action
        self._passphrase = passphrase
        self._is_mainnet = is_mainnet
        self._last_nonce = 0

    def new_agent(self) -> Tuple[str, str]:
        """Fresh agent wallet (never reused): (address, encrypted keystore JSON)."""
        key = "0x" + secrets.token_hex(32)
        address = self._account.from_key(key).address
        keystore = self._account.encrypt(key, self._passphrase)
        return address, json.dumps(keystore)

    def next_nonce(self) -> int:
        nonce = max(int(time.time() * 1000), self._last_nonce + 1)
        self._last_nonce = nonce
        return nonce

    def sign_l1(self, keystore_json: str, action: Mapping[str, Any], nonce: int) -> Dict[str, Any]:
        key = self._account.decrypt(json.loads(keystore_json), self._passphrase)
        wallet = self._account.from_key(key)
        return self._sign_l1_action(wallet, action, None, nonce, None, self._is_mainnet)
