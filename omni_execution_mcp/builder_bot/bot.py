"""Telegram bot in Spanish for Hyperliquid perps with a white-label builder code.

Orders go straight to Hyperliquid's /info and /exchange: there is no x402
preflight on the trade path. Defaults are testnet and dry-run. In live mode each
trader signs through an agent wallet that can trade but never withdraw.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Tuple

from ..config import MAX_HYPERLIQUID_PERP_TENTHS_BPS, _parse_fee_int, _validate_evm_address
from . import hl
from .ledger import Group, Ledger, Trader, month_bounds_ms

log = logging.getLogger("omni_builder_bot")

PENDING_TTL_S = 60
TELEGRAM_TEXT_MAX = 4096
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
PRIVATE_KEY_RE = re.compile(r"(?<![0-9a-fA-F])(?:0x)?[0-9a-fA-F]{64}(?![0-9a-fA-F])")
USD_RE = re.compile(r"^\$?(\d+)(?:[.,](\d{1,2}))?$")
PERIOD_RE = re.compile(r"^(\d{4})-(\d{2})$")
TRUE_VALUES = {"1", "true", "si", "sí"}

KEY_WARNING = (
    "⚠️ Eso parece una clave privada. No la guardé ni la voy a usar.\n"
    "Este bot nunca te pide claves ni frases semilla. Borrá ese mensaje y, si la clave era real, "
    "considerala comprometida: mové tus fondos a una wallet nueva."
)
HL_DOWN = "No pude hablar con Hyperliquid. Probá en un rato."
NEED_WALLET = "Primero vinculá tu cuenta con /wallet 0x… (solo la dirección pública)."
NEED_AGENT = "Primero mandá /activar y firmá el link con tu wallet."
ORDER_UNKNOWN = (
    "Se cortó la conexión con Hyperliquid y no sé si la orden entró. "
    "Revisá tus posiciones antes de reintentar."
)
AGENT_LOCKED = "No pude abrir tu agente; mandá /activar y firmá de nuevo."


def _pct(tenths_bps: int) -> str:
    return f"{(Decimal(tenths_bps) / 1000).normalize():f}%"


def _parse_usd(text: str) -> Decimal:
    match = USD_RE.match(text)
    if not match:
        raise ValueError("El monto va en USD sin separador de miles, por ejemplo 25, 25.5 o 25,5.")
    return Decimal(f"{match.group(1)}.{match.group(2) or '0'}")


def _validate_sign_page_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    local = parts.hostname in ("localhost", "127.0.0.1")
    if "#" in url or not parts.hostname or not (
        parts.scheme == "https" or (parts.scheme == "http" and local)
    ):
        raise ValueError(
            "SIGN_PAGE_URL tiene que ser https://… (o http://localhost para probar) y sin #."
        )
    return url


def _parse_admin_ids(raw: str) -> FrozenSet[int]:
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise ValueError(
                f"BOT_ADMIN_TG_IDS tiene un ID inválido: {part!r}. Usá números separados por coma."
            )
        ids.add(int(part))
    return frozenset(ids)


@dataclass(frozen=True)
class BotConfig:
    token: str = field(repr=False)
    username: str
    network: str
    live: bool
    builder_address: str
    builder_tenths_bps: int
    db_path: str
    keystore_passphrase: Optional[str] = field(repr=False)
    sign_page_url: str
    admin_tg_ids: FrozenSet[int]

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "BotConfig":
        source = os.environ if env is None else env

        def get(name: str, default: str = "") -> str:
            return (source.get(name) or default).strip()

        token = get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise ValueError("Falta TELEGRAM_BOT_TOKEN.")
        username = get("TELEGRAM_BOT_USERNAME").lstrip("@")
        if not USERNAME_RE.match(username):
            raise ValueError("TELEGRAM_BOT_USERNAME tiene que ser el usuario del bot, sin espacios.")
        network = get("HL_NETWORK", "testnet").lower()
        if network not in hl.API_URLS:
            raise ValueError(f"HL_NETWORK tiene que ser testnet o mainnet, no {network!r}.")
        builder = _validate_evm_address(
            "HYPERLIQUID_BUILDER_ADDRESS", source.get("HYPERLIQUID_BUILDER_ADDRESS")
        )
        if not builder or int(builder, 16) == 0:
            raise ValueError(
                "Falta HYPERLIQUID_BUILDER_ADDRESS: la dirección pública que cobra el builder fee "
                "(la dirección cero de .env.example no sirve)."
            )
        tenths = hl.validate_perp_tenths(
            _parse_fee_int(
                "HYPERLIQUID_BUILDER_TENTHS_BPS",
                source.get("HYPERLIQUID_BUILDER_TENTHS_BPS"),
                default=30,
                max_cap=MAX_HYPERLIQUID_PERP_TENTHS_BPS,
            )
        )
        return cls(
            token=token,
            username=username,
            network=network,
            live=get("BOT_LIVE").lower() in TRUE_VALUES,
            builder_address=builder,
            builder_tenths_bps=tenths,
            db_path=get("BOT_DB_PATH", "builder_bot.db"),
            keystore_passphrase=source.get("BOT_KEYSTORE_PASSPHRASE") or None,
            sign_page_url=_validate_sign_page_url(get("SIGN_PAGE_URL")),
            admin_tg_ids=_parse_admin_ids(get("BOT_ADMIN_TG_IDS")),
        )


class BuilderBot:
    """Spanish command handler: one reply text per private message."""

    def __init__(
        self,
        config: BotConfig,
        ledger: Ledger,
        client: hl.HyperliquidClient,
        signer: Optional[hl.AgentSigner] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.ledger = ledger
        self.client = client
        self.signer = signer
        self.clock = clock
        self._pending: Dict[int, Tuple[float, hl.OrderPreview]] = {}
        self._commands: Dict[str, Callable[[int, List[str]], str]] = {
            "start": self._start,
            "ayuda": self._ayuda,
            "help": self._ayuda,
            "wallet": self._wallet,
            "activar": self._activar,
            "verificar": self._verificar,
            "long": lambda tg_id, args: self._open(tg_id, args, True),
            "short": lambda tg_id, args: self._open(tg_id, args, False),
            "cerrar": self._cerrar,
            "confirmar": self._confirmar,
            "cancelar": self._cancelar,
            "grupo_nuevo": self._grupo_nuevo,
            "mis_grupos": self._mis_grupos,
            "cobro": self._cobro,
            "liquidacion": self._liquidacion,
        }

    def handle_update(self, update: Mapping[str, Any]) -> Optional[Tuple[int, str]]:
        """(chat_id, reply) for private text messages; anything else is ignored."""
        message = update.get("message")
        if not isinstance(message, Mapping):
            return None
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        text = message.get("text")
        if chat.get("type") != "private" or not isinstance(text, str) or "id" not in sender:
            return None
        return int(chat["id"]), self.handle(int(sender["id"]), text)

    def handle(self, tg_id: int, text: str) -> str:
        if PRIVATE_KEY_RE.search(text):
            return KEY_WARNING
        parts = text.strip().split()
        if not parts or not parts[0].startswith("/"):
            return "Mandá /ayuda para ver los comandos."
        command = parts[0][1:].split("@", 1)[0].lower()
        handler = self._commands.get(command)
        if handler is None:
            return "No conozco ese comando. Mandá /ayuda."
        try:
            return handler(tg_id, parts[1:])
        except (json.JSONDecodeError, OSError):
            return HL_DOWN
        except ValueError as exc:
            return str(exc)
        except Exception as exc:
            log.error("Error inesperado en /%s: %s", command, type(exc).__name__)
            return "Algo salió mal; probá de nuevo."

    def _red(self) -> str:
        return "Mainnet (fondos reales)" if self.config.network == "mainnet" else "Testnet (sin fondos reales)"

    def _trader_with_wallet(self, tg_id: int) -> Trader:
        trader = self.ledger.get_trader(tg_id)
        if trader is None or not trader.hl_address:
            raise ValueError(NEED_WALLET)
        return trader

    def _trader_for_orders(self, tg_id: int) -> Trader:
        trader = self._trader_with_wallet(tg_id)
        if self.config.live and not trader.agent_address:
            raise ValueError(NEED_AGENT)
        return trader

    def _own_group(self, tg_id: int, raw_id: str) -> Group:
        group = self.ledger.get_group(raw_id.removeprefix("grp_"))
        if group is None or group.admin_tg_id != tg_id:
            raise ValueError("No encuentro esa comunidad entre las tuyas. Mirá /mis_grupos.")
        return group

    def _invite_link(self, group: Group) -> str:
        return f"https://t.me/{self.config.username}?start=grp_{group.id}"

    def _start(self, tg_id: int, args: List[str]) -> str:
        notice = ""
        group: Optional[Group] = None
        if args and args[0].startswith("grp_"):
            group = self.ledger.get_group(args[0][len("grp_"):])
            if group is None:
                notice = "Ese link de comunidad no existe; te registro sin comunidad.\n\n"
        trader = self.ledger.register_trader(tg_id, group.id if group else None)
        if group and trader.group_id != group.id:
            notice = "Ya estabas registrado por otra comunidad; eso no cambia.\n\n"
        elif group:
            notice = f"Entraste por la comunidad «{group.name}».\n\n"
        modo = "real: las órdenes se envían" if self.config.live else "simulación: no se envía ninguna orden"
        return notice + (
            "Hola 👋 Opero perps de Hyperliquid por vos desde Telegram.\n\n"
            "1. /wallet 0x… — tu cuenta principal de Hyperliquid (solo la dirección pública).\n"
            "2. /activar — te paso un link para firmar con tu wallet.\n"
            "3. /verificar — reviso que haya quedado todo autorizado.\n"
            "4. /long BTC 25 o /short ETH 50 — monto en USD; siempre ves la orden antes de /confirmar.\n\n"
            f"Red: {self._red()}. Modo {modo}.\n"
            f"Comisión del bot: {_pct(self.config.builder_tenths_bps)} del monto de cada orden, "
            "además de las comisiones de Hyperliquid.\n"
            "Nunca te voy a pedir tu clave privada ni tu frase semilla."
        )

    def _ayuda(self, tg_id: int, args: List[str]) -> str:
        text = (
            "Comandos:\n"
            "/wallet 0x… — vincular tu cuenta de Hyperliquid\n"
            "/activar — link para autorizar la comisión y el agente\n"
            "/verificar — revisar las autorizaciones\n"
            "/long MONEDA USD — abrir un long, ej. /long BTC 25\n"
            "/short MONEDA USD — abrir un short\n"
            "/cerrar MONEDA — cerrar tu posición\n"
            f"/confirmar — enviar la orden pendiente (vale {PENDING_TTL_S} s)\n"
            "/cancelar — descartar la orden pendiente"
        )
        if tg_id in self.config.admin_tg_ids or self.ledger.groups_for_admin(tg_id):
            text += (
                "\n\nAdmins de comunidad:\n"
                "/grupo_nuevo NOMBRE — crear tu link de invitación\n"
                "/mis_grupos — tus comunidades y traders\n"
                "/cobro ID 0x… — dirección donde cobrás tu parte\n"
                "/liquidacion ID [AAAA-MM] — comisiones del mes y tu parte"
            )
        return text

    def _wallet(self, tg_id: int, args: List[str]) -> str:
        if len(args) != 1 or not ADDRESS_RE.match(args[0]):
            return (
                "Usá /wallet 0x… con la dirección pública (0x y 40 caracteres) "
                "de tu cuenta principal de Hyperliquid."
            )
        address = args[0].lower()
        before = self.ledger.get_trader(tg_id)
        self.ledger.link_wallet(tg_id, address)
        if before and before.hl_address and before.hl_address != address:
            return f"Cambiaste tu cuenta a {address}. El agente anterior quedó sin uso: mandá /activar de nuevo."
        return f"Cuenta vinculada: {address}. Ahora mandá /activar."

    def _activar(self, tg_id: int, args: List[str]) -> str:
        trader = self._trader_with_wallet(tg_id)
        cfg = self.config
        fee = _pct(cfg.builder_tenths_bps)
        if self.signer is None:
            url = hl.sign_page_url(
                cfg.sign_page_url, cfg.network, trader.hl_address,
                builder=cfg.builder_address, tenths_bps=cfg.builder_tenths_bps,
            )
            return (
                f"Abrí este link en el navegador donde tenés tu wallet y firmá la comisión del builder (hasta {fee}):\n"
                f"{url}\n\n"
                "Este bot corre en simulación sin agentes: todavía no puede operar por vos. "
                "Cuando firmes, mandá /verificar."
            )
        agent_address, keystore = self.signer.new_agent()
        self.ledger.set_agent(tg_id, agent_address, keystore)
        url = hl.sign_page_url(
            cfg.sign_page_url, cfg.network, trader.hl_address,
            builder=cfg.builder_address, tenths_bps=cfg.builder_tenths_bps, agent=agent_address,
        )
        return (
            "Abrí este link en el navegador donde tenés tu wallet (en el celular, el navegador de MetaMask o Rabby) "
            f"y firmá dos veces:\n{url}\n\n"
            f"1. Comisión del builder: hasta {fee} del monto de cada orden que mandes con el bot.\n"
            f"2. Agente «{hl.AGENT_NAME}»: abre y cierra posiciones por vos, pero no puede retirar ni transferir fondos.\n\n"
            "Cuando termines, mandá /verificar."
        )

    def _verificar(self, tg_id: int, args: List[str]) -> str:
        trader = self._trader_with_wallet(tg_id)
        cfg = self.config
        approved = self.client.max_builder_fee(trader.hl_address, cfg.builder_address)
        fee_ok = approved >= cfg.builder_tenths_bps
        lines = [
            ("✅" if fee_ok else "❌")
            + f" Comisión del builder: autorizaste hasta {_pct(approved)}; el bot cobra {_pct(cfg.builder_tenths_bps)}."
        ]
        if self.signer is None:
            agent_ok = True
            lines.append("ℹ️ Sin agente: este bot corre en simulación.")
        elif trader.agent_address:
            agent_ok = self.client.agent_owner(trader.agent_address) == trader.hl_address
            lines.append(
                ("✅" if agent_ok else "❌")
                + f" Agente «{hl.AGENT_NAME}»: "
                + ("autorizado." if agent_ok else "todavía sin autorizar.")
            )
        else:
            agent_ok = False
            lines.append("❌ Todavía no tenés agente: mandá /activar.")
        if fee_ok and agent_ok:
            lines.append("Todo listo: probá /long BTC 25.")
        else:
            lines.append("Si recién firmaste, esperá unos segundos y mandá /verificar otra vez; si no, repetí /activar.")
        return "\n".join(lines)

    def _open(self, tg_id: int, args: List[str], is_buy: bool) -> str:
        verb = "long" if is_buy else "short"
        if len(args) != 2:
            return f"Usá /{verb} MONEDA USD, por ejemplo /{verb} BTC 25."
        self._trader_for_orders(tg_id)
        usd = _parse_usd(args[1])
        cfg = self.config
        preview = hl.open_order(
            self.client.meta(), self.client.all_mids(), args[0], is_buy, usd,
            cfg.builder_address, cfg.builder_tenths_bps,
        )
        return self._stage(tg_id, preview)

    def _cerrar(self, tg_id: int, args: List[str]) -> str:
        if len(args) != 1:
            return "Usá /cerrar MONEDA, por ejemplo /cerrar BTC."
        trader = self._trader_for_orders(tg_id)
        cfg = self.config
        preview = hl.close_order(
            self.client.meta(), self.client.all_mids(),
            self.client.clearinghouse_state(trader.hl_address), args[0],
            cfg.builder_address, cfg.builder_tenths_bps,
        )
        return self._stage(tg_id, preview)

    def _stage(self, tg_id: int, preview: hl.OrderPreview) -> str:
        self._pending[tg_id] = (self.clock() + PENDING_TTL_S, preview)
        if preview.reduce_only:
            title = f"Cerrar {'short' if preview.is_buy else 'long'} {preview.coin}"
        else:
            title = f"{'LONG' if preview.is_buy else 'SHORT'} {preview.coin}"
        side = "sobre" if preview.is_buy else "bajo"
        lines = [
            f"Orden a confirmar en {self._red()}:",
            f"{title} · tamaño {hl.to_wire(preview.size)} (≈ {preview.notional_usd:.2f} USD)",
            f"Precio límite {hl.to_wire(preview.limit_px)}: se ejecuta ya o se cancela, "
            f"hasta 1% {side} el precio medio {preview.mid}.",
            f"Comisión del bot: {preview.builder_fee_usd:.4f} USD ({_pct(preview.tenths_bps)}), "
            "además de las de Hyperliquid.",
        ]
        if not self.config.live:
            lines.append("Modo simulación: no se va a enviar.")
        lines.append(f"Mandá /confirmar en los próximos {PENDING_TTL_S} s, o /cancelar.")
        return "\n".join(lines)

    def _confirmar(self, tg_id: int, args: List[str]) -> str:
        pending = self._pending.pop(tg_id, None)
        if pending is None:
            return "No tenés ninguna orden pendiente. Armala con /long, /short o /cerrar."
        expires_at, preview = pending
        if self.clock() > expires_at:
            return f"La orden venció ({PENDING_TTL_S} s). Armala de nuevo para tomar el precio actual."
        if not self.config.live:
            return "Simulación: esta orden NO se envió.\n" + json.dumps(preview.action, separators=(",", ":"))
        trader = self._trader_for_orders(tg_id)
        if self.signer is None or not trader.agent_keystore:
            raise ValueError(NEED_AGENT)
        nonce = self.signer.next_nonce()
        try:
            signature = self.signer.sign_l1(trader.agent_keystore, preview.action, nonce)
        except ValueError:
            raise ValueError(AGENT_LOCKED) from None
        try:
            response = self.client.exchange(hl.exchange_payload(preview.action, nonce, signature))
        except (json.JSONDecodeError, OSError):
            raise ValueError(ORDER_UNKNOWN) from None
        estado, oid, detalle = hl.parse_order_response(response)
        if oid is not None:
            self.ledger.record_order(
                trader.hl_address, oid, tg_id, trader.group_id,
                preview.coin, preview.is_buy, preview.tenths_bps,
            )
        if estado == "filled":
            return f"✅ Orden ejecutada: {detalle}."
        if estado == "resting":
            return f"La orden quedó en el libro (oid {oid})."
        return f"Hyperliquid rechazó la orden: {detalle}"

    def _cancelar(self, tg_id: int, args: List[str]) -> str:
        if self._pending.pop(tg_id, None) is None:
            return "No había ninguna orden pendiente."
        return "Listo, descarté la orden."

    def _grupo_nuevo(self, tg_id: int, args: List[str]) -> str:
        if tg_id not in self.config.admin_tg_ids:
            return "Solo los admins habilitados por el operador pueden crear comunidades."
        name = " ".join(args)
        if not name or len(name) > 64:
            return "Usá /grupo_nuevo NOMBRE (hasta 64 caracteres)."
        group = self.ledger.create_group(name, tg_id)
        return (
            f"Comunidad «{group.name}» creada (ID {group.id}).\n"
            f"Link de invitación: {self._invite_link(group)}\n"
            f"Tu parte: {group.share_bps / 100:g}% de las comisiones de builder de las órdenes que hagan con el bot.\n"
            f"Cargá dónde cobrar con /cobro {group.id} 0x…"
        )

    def _mis_grupos(self, tg_id: int, args: List[str]) -> str:
        groups = self.ledger.groups_for_admin(tg_id)
        if not groups:
            return "No tenés comunidades. Si sos admin habilitado, creá una con /grupo_nuevo NOMBRE."
        lines = ["Tus comunidades:"]
        for group in groups:
            lines.append(
                f"• {group.name} (ID {group.id}) · traders: {self.ledger.count_group_traders(group.id)} · "
                f"cobro: {group.payout_address or 'sin cargar (/cobro)'}\n  {self._invite_link(group)}"
            )
        return "\n".join(lines)

    def _cobro(self, tg_id: int, args: List[str]) -> str:
        if len(args) != 2 or not ADDRESS_RE.match(args[1]):
            return "Usá /cobro ID 0x… con la dirección pública donde querés cobrar."
        group = self._own_group(tg_id, args[0])
        self.ledger.set_payout_address(group.id, args[1])
        return (
            f"Listo: tu parte de «{group.name}» se liquida a {args[1].lower()}.\n"
            "El bot solo calcula; los pagos los hace el operador."
        )

    def _liquidacion(self, tg_id: int, args: List[str]) -> str:
        if not 1 <= len(args) <= 2:
            return "Usá /liquidacion ID [AAAA-MM], por ejemplo /liquidacion ab12cd34 2026-11."
        group = self._own_group(tg_id, args[0])
        if len(args) == 2:
            match = PERIOD_RE.match(args[1])
            if not match:
                return "El período va como AAAA-MM, por ejemplo 2026-11."
            year, month = int(match.group(1)), int(match.group(2))
        else:
            now = datetime.fromtimestamp(self.clock(), tz=timezone.utc)
            year, month = now.year, now.month
        start_ms, end_ms = month_bounds_ms(year, month)
        for address in self.ledger.group_trader_addresses(group.id):
            self.ledger.ingest_fills(address, self.client.fills_between(address, start_ms, end_ms))
        st = self.ledger.statement(group.id, start_ms, end_ms)
        return (
            f"Liquidación {year}-{month:02d} · «{group.name}»\n"
            f"Fills del bot: {st.fills} · volumen {st.volume_usd:.2f} USD\n"
            f"Comisiones de builder: {st.builder_fees_usd:.6f} USDC\n"
            f"Tu parte ({st.share_bps / 100:g}%): {st.admin_share_usd:.6f} USDC\n"
            f"Operador: {st.operator_net_usd:.6f} USDC\n"
            f"Cobro: {st.payout_address or 'sin cargar (/cobro)'}\n"
            "Cálculo informativo: no se transfirió nada."
        )


class TelegramError(RuntimeError):
    """Telegram API failure; the message never contains the bot token."""


TelegramTransport = Callable[[str, bytes, float], bytes]


def _telegram_transport(url: str, body: bytes, timeout: float) -> bytes:
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


class TelegramApi:
    def __init__(self, token: str, transport: Optional[TelegramTransport] = None) -> None:
        self._base = f"https://api.telegram.org/bot{token}/"
        self._transport = transport or _telegram_transport

    def __repr__(self) -> str:
        return "TelegramApi(token=<oculto>)"

    def call(self, method: str, payload: Mapping[str, Any], timeout: float = 30) -> Any:
        try:
            raw = self._transport(self._base + method, json.dumps(payload).encode("utf-8"), timeout)
            data = json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise TelegramError(f"Telegram respondió HTTP {exc.code} a {method}.") from None
        except (OSError, ValueError) as exc:
            raise TelegramError(f"No pude hablar con Telegram en {method} ({type(exc).__name__}).") from None
        if not isinstance(data, Mapping) or not data.get("ok"):
            detail = data.get("description", "sin detalle") if isinstance(data, Mapping) else "respuesta inválida"
            raise TelegramError(f"Telegram rechazó {method}: {detail}")
        return data.get("result")

    def get_updates(self, offset: Optional[int], timeout: int = 25) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", payload, timeout=timeout + 10)

    def send_message(self, chat_id: int, text: str) -> None:
        self.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text[:TELEGRAM_TEXT_MAX],
                "link_preview_options": {"is_disabled": True},
            },
        )


def run_polling(
    api: TelegramApi,
    bot: BuilderBot,
    sleep: Callable[[float], None] = time.sleep,
    max_cycles: Optional[int] = None,
) -> None:
    """Long polling with exponential backoff; a failing update never stops the loop."""
    offset: Optional[int] = None
    backoff = 1.0
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        try:
            updates = api.get_updates(offset)
        except TelegramError as exc:
            log.warning("%s Reintento en %.0f s.", exc, backoff)
            sleep(backoff)
            backoff = min(backoff * 2, 60.0)
            continue
        backoff = 1.0
        for update in updates:
            offset = int(update["update_id"]) + 1
            try:
                reply = bot.handle_update(update)
                if reply is not None:
                    api.send_message(*reply)
            except Exception as exc:
                log.error("No pude procesar el update %s: %s", update.get("update_id"), type(exc).__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = BotConfig.from_env()
    except ValueError as exc:
        print(f"Configuración inválida: {exc}", file=sys.stderr)
        return 2
    signer: Optional[hl.AgentSigner] = None
    if config.keystore_passphrase:
        try:
            signer = hl.AgentSigner(config.keystore_passphrase, config.network == "mainnet")
        except RuntimeError as exc:
            if config.live:
                print(f"No puedo operar en vivo: {exc}", file=sys.stderr)
                return 2
            log.warning("%s. Sigo en simulación sin agentes.", exc)
    elif config.live:
        print("BOT_LIVE=1 necesita BOT_KEYSTORE_PASSPHRASE para cifrar las agent wallets.", file=sys.stderr)
        return 2
    ledger = Ledger(config.db_path)
    bot = BuilderBot(config, ledger, hl.HyperliquidClient(config.network), signer)
    log.info(
        "Bot @%s · red %s · %s · builder %s · fee %s",
        config.username,
        config.network,
        "EN VIVO" if config.live else "simulación",
        config.builder_address,
        _pct(config.builder_tenths_bps),
    )
    try:
        run_polling(TelegramApi(config.token), bot)
    except KeyboardInterrupt:
        log.info("Bot detenido.")
    finally:
        ledger.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
