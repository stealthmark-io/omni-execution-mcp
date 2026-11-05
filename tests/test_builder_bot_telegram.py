import calendar
import contextlib
import io
import json
import os
import traceback
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from omni_execution_mcp.builder_bot import bot as botmod
from omni_execution_mcp.builder_bot import hl
from omni_execution_mcp.builder_bot.bot import (
    KEY_WARNING,
    BotConfig,
    BuilderBot,
    TelegramApi,
    TelegramError,
    run_polling,
)
from omni_execution_mcp.builder_bot.ledger import Ledger, month_bounds_ms
from tests.test_builder_bot import (
    BUILDER_ADDR,
    META,
    MIDS,
    OTHER_ADDR,
    PAYOUT_ADDR,
    TRADER_ADDR,
    _fill,
)

TESTNET = hl.API_URLS["testnet"]
AGENT_ADDR = "0x" + "a9" * 20
ADMIN_TG = 1
TRADER_TG = 100
NOW = calendar.timegm((2026, 11, 15, 12, 0, 0))
FAKE_TOKEN = "000000:token-falso-solo-tests"
ENV = {
    "TELEGRAM_BOT_TOKEN": FAKE_TOKEN,
    "TELEGRAM_BOT_USERNAME": "@OmniTraderBot",
    "HYPERLIQUID_BUILDER_ADDRESS": BUILDER_ADDR,
    "SIGN_PAGE_URL": "https://firmar.example.com/firmar.html",
    "BOT_ADMIN_TG_IDS": "1, 2",
}


def filled(oid, total="0.00051", avg="97200.0"):
    status = {"filled": {"totalSz": total, "avgPx": avg, "oid": oid}}
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": [status]}}}


def order_status(status):
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": [status]}}}


def order_count(ledger):
    return ledger._db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]


def sign_link_params(reply):
    link = next(word for word in reply.split() if word.startswith("https://firmar.example.com/"))
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(link).fragment))


def private_update(update_id, text, chat_id=TRADER_TG):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id, "type": "private"}, "from": {"id": TRADER_TG}, "text": text},
    }


class RoutingTransport:
    """Fake HL API: /info answers by request type, /exchange pops a queue; every call is recorded."""

    def __init__(self):
        self.info = {
            "meta": META,
            "allMids": MIDS,
            "maxBuilderFee": 30,
            "userRole": {"role": "agent", "data": {"user": TRADER_ADDR}},
            "clearinghouseState": {"assetPositions": []},
            "userFillsByTime": [],
        }
        self.exchange = []
        self.calls = []

    def __call__(self, url, body):
        payload = json.loads(body)
        self.calls.append((url, payload))
        if url == TESTNET + "/info":
            response = self.info[payload["type"]]
        elif url == TESTNET + "/exchange":
            response = self.exchange.pop(0)
        else:
            raise AssertionError(f"URL inesperada: {url}")
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, bytes):
            return response
        return json.dumps(response).encode("utf-8")

    def queries(self, request_type):
        return [payload for url, payload in self.calls if payload.get("type") == request_type]

    def exchange_calls(self):
        return [payload for url, payload in self.calls if url.endswith("/exchange")]


class FakeSigner:
    def __init__(self):
        self.nonce = 1_763_000_000_000
        self.signed = []

    def new_agent(self):
        return "0x" + "A9" * 20, "keystore-cifrado"

    def next_nonce(self):
        self.nonce += 1
        return self.nonce

    def sign_l1(self, keystore_json, action, nonce):
        self.signed.append((keystore_json, action, nonce))
        return {"r": "0x1", "s": "0x2", "v": 27}


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class BotCase(unittest.TestCase):
    env_overrides = {}
    with_signer = True

    def setUp(self):
        self.config = BotConfig.from_env({**ENV, **self.env_overrides})
        self.ledger = Ledger(":memory:")
        self.hl_api = RoutingTransport()
        self.signer = FakeSigner() if self.with_signer else None
        self.clock = FakeClock(NOW)
        client = hl.HyperliquidClient("testnet", transport=self.hl_api)
        self.bot = BuilderBot(self.config, self.ledger, client, self.signer, clock=self.clock)

    def tearDown(self):
        self.ledger.close()

    def say(self, text, tg_id=TRADER_TG):
        return self.bot.handle(tg_id, text)

    def onboard(self, start="/start"):
        self.say(start)
        self.say(f"/wallet {TRADER_ADDR}")
        self.say("/activar")


class TestOnboarding(BotCase):
    def test_group_link_attribution_is_first_touch(self):
        group = self.ledger.create_group("Señales AR", ADMIN_TG)
        other = self.ledger.create_group("Otra comunidad", 2)
        self.assertIn("«Señales AR»", self.say(f"/start grp_{group.id}"))
        self.assertIn("Ya estabas registrado", self.say(f"/start grp_{other.id}"))
        self.assertEqual(self.ledger.get_trader(TRADER_TG).group_id, group.id)

    def test_unknown_group_link_registers_without_group(self):
        self.assertIn("no existe", self.say("/start grp_deadbeef"))
        self.assertIsNone(self.ledger.get_trader(TRADER_TG).group_id)

    def test_welcome_states_network_mode_and_fee(self):
        reply = self.say("/start@OmniTraderBot")
        self.assertIn("Testnet", reply)
        self.assertIn("Modo simulación", reply)
        self.assertIn("0.03% del monto de cada orden, además de las comisiones de Hyperliquid", reply)

    def test_wallet_is_validated_and_lowercased(self):
        self.assertIn("Usá /wallet", self.say("/wallet 0x123"))
        self.say("/wallet 0x" + "AB" * 20)
        self.assertEqual(self.ledger.get_trader(TRADER_TG).hl_address, TRADER_ADDR)

    def test_changing_wallet_drops_agent(self):
        self.onboard()
        self.assertIn("/activar de nuevo", self.say(f"/wallet {OTHER_ADDR}"))
        self.assertIsNone(self.ledger.get_trader(TRADER_TG).agent_address)

    def test_activar_needs_wallet(self):
        self.assertEqual(self.say("/activar"), botmod.NEED_WALLET)

    def test_activar_creates_agent_and_signing_link(self):
        self.say(f"/wallet {TRADER_ADDR}")
        reply = self.say("/activar")
        trader = self.ledger.get_trader(TRADER_TG)
        self.assertEqual((trader.agent_address, trader.agent_keystore), (AGENT_ADDR, "keystore-cifrado"))
        self.assertEqual(
            sign_link_params(reply),
            {
                "red": "testnet",
                "cuenta": TRADER_ADDR,
                "builder": BUILDER_ADDR.lower(),
                "tasa": "0.03%",
                "agente": AGENT_ADDR,
                "nombre": "omnibot",
            },
        )
        self.assertIn("no puede retirar ni transferir fondos", reply)
        self.assertEqual(self.hl_api.calls, [])


class TestVerificar(BotCase):
    def test_all_approved(self):
        self.onboard()
        reply = self.say("/verificar")
        self.assertIn("✅ Comisión del builder: autorizaste hasta 0.03%", reply)
        self.assertIn("✅ Agente «omnibot»: autorizado.", reply)
        self.assertIn("Todo listo: probá /long BTC 25.", reply)
        self.assertEqual(
            self.hl_api.queries("maxBuilderFee"),
            [{"type": "maxBuilderFee", "user": TRADER_ADDR, "builder": BUILDER_ADDR.lower()}],
        )
        self.assertEqual(self.hl_api.queries("userRole"), [{"type": "userRole", "user": AGENT_ADDR}])

    def test_lower_fee_or_foreign_agent_fail(self):
        self.onboard()
        self.hl_api.info["maxBuilderFee"] = 10
        self.hl_api.info["userRole"] = {"role": "agent", "data": {"user": OTHER_ADDR}}
        reply = self.say("/verificar")
        self.assertIn("❌ Comisión del builder: autorizaste hasta 0.01%", reply)
        self.assertIn("❌ Agente «omnibot»: todavía sin autorizar.", reply)
        self.assertNotIn("Todo listo", reply)

    def test_agent_not_registered_yet(self):
        self.onboard()
        self.hl_api.info["userRole"] = {"role": "missing"}
        self.assertIn("todavía sin autorizar", self.say("/verificar"))


class TestWithoutSigner(BotCase):
    with_signer = False

    def test_activar_links_only_the_builder_fee(self):
        self.say(f"/wallet {TRADER_ADDR}")
        reply = self.say("/activar")
        params = sign_link_params(reply)
        self.assertEqual(params["tasa"], "0.03%")
        self.assertNotIn("agente", params)
        self.assertIn("simulación sin agentes", reply)

    def test_verificar_only_checks_the_fee(self):
        self.say(f"/wallet {TRADER_ADDR}")
        self.assertIn("Todo listo", self.say("/verificar"))
        self.assertEqual([payload["type"] for _, payload in self.hl_api.calls], ["maxBuilderFee"])


class TestDryRunOrders(BotCase):
    def setUp(self):
        super().setUp()
        self.onboard()

    def test_long_preview_then_confirm_sends_nothing(self):
        preview = self.say("/long btc 50")
        self.assertIn("LONG BTC · tamaño 0.00051 (≈ 49.53 USD)", preview)
        self.assertIn("Precio límite 98095", preview)
        self.assertIn("hasta 1% sobre el precio medio 97123.5", preview)
        self.assertIn("Comisión del bot: 0.0149 USD (0.03%)", preview)
        self.assertIn("Modo simulación: no se va a enviar.", preview)
        reply = self.say("/confirmar")
        header, wire = reply.split("\n", 1)
        self.assertEqual(header, "Simulación: esta orden NO se envió.")
        action = json.loads(wire)
        self.assertEqual(action["builder"], {"b": BUILDER_ADDR.lower(), "f": 30})
        self.assertEqual(
            action["orders"],
            [{"a": 0, "b": True, "p": "98095", "s": "0.00051", "r": False, "t": {"limit": {"tif": "Ioc"}}}],
        )
        self.assertEqual(self.hl_api.exchange_calls(), [])
        self.assertEqual(self.signer.signed, [])
        self.assertEqual(order_count(self.ledger), 0)

    def test_short_preview_is_below_mid(self):
        preview = self.say("/short ETH 25,5")
        self.assertIn("SHORT ETH · tamaño 0.0073", preview)
        self.assertIn("Precio límite 3422.2", preview)
        self.assertIn("hasta 1% bajo el precio medio 3456.78", preview)

    def test_invalid_orders_explain_why(self):
        self.assertIn("El mínimo por orden en Hyperliquid es 10 USD.", self.say("/long BTC 5"))
        self.assertIn("El monto va en USD", self.say("/long BTC 1.000,50"))
        self.assertIn("No encuentro el perp DOGE", self.say("/long DOGE 25"))
        self.assertIn("Usá /long MONEDA USD", self.say("/long BTC"))
        self.assertIn("No tenés ninguna orden pendiente", self.say("/confirmar"))

    def test_preview_expires_after_60_seconds(self):
        self.say("/long BTC 50")
        self.clock.now += 61
        self.assertIn("venció", self.say("/confirmar"))
        self.assertIn("No tenés ninguna orden pendiente", self.say("/confirmar"))

    def test_cancelar_drops_the_pending_order(self):
        self.say("/long BTC 50")
        self.assertEqual(self.say("/cancelar"), "Listo, descarté la orden.")
        self.assertIn("No tenés ninguna orden pendiente", self.say("/confirmar"))
        self.assertEqual(self.say("/cancelar"), "No había ninguna orden pendiente.")

    def test_cerrar_builds_reduce_only_opposite_order(self):
        self.hl_api.info["clearinghouseState"] = {
            "assetPositions": [{"position": {"coin": "BTC", "szi": "-0.002"}}]
        }
        self.assertIn("Cerrar short BTC · tamaño 0.002", self.say("/cerrar BTC"))
        order = json.loads(self.say("/confirmar").split("\n", 1)[1])["orders"][0]
        self.assertEqual((order["b"], order["r"], order["s"]), (True, True, "0.002"))
        self.assertEqual(self.hl_api.queries("clearinghouseState")[0]["user"], TRADER_ADDR)

    def test_cerrar_without_position(self):
        self.assertIn("No tenés posición abierta en BTC.", self.say("/cerrar BTC"))


class TestLiveOrders(BotCase):
    env_overrides = {"BOT_LIVE": "1"}

    def setUp(self):
        super().setUp()
        self.group = self.ledger.create_group("Señales AR", ADMIN_TG)
        self.onboard(start=f"/start grp_{self.group.id}")

    def test_welcome_says_orders_are_real(self):
        self.assertIn("Modo real", self.say("/start"))

    def test_confirm_signs_with_agent_and_records_order_for_group(self):
        self.hl_api.exchange.append(filled(77))
        self.say("/long BTC 50")
        self.assertEqual(self.say("/confirmar"), "✅ Orden ejecutada: 0.00051 @ 97200.0.")
        [(keystore, action, nonce)] = self.signer.signed
        self.assertEqual(keystore, "keystore-cifrado")
        self.assertEqual(action["builder"], {"b": BUILDER_ADDR.lower(), "f": 30})
        self.assertEqual(
            self.hl_api.exchange_calls(),
            [
                {
                    "action": action,
                    "nonce": nonce,
                    "signature": {"r": "0x1", "s": "0x2", "v": 27},
                    "vaultAddress": None,
                    "expiresAfter": None,
                }
            ],
        )
        self.assertEqual(self.ledger.group_trader_addresses(self.group.id), [TRADER_ADDR])

    def test_trade_path_only_talks_to_hyperliquid_testnet(self):
        self.hl_api.exchange.append(filled(77))
        self.say("/long BTC 50")
        self.say("/confirmar")
        self.assertEqual(
            {url for url, _ in self.hl_api.calls}, {TESTNET + "/info", TESTNET + "/exchange"}
        )

    def test_resting_order_is_recorded(self):
        self.hl_api.exchange.append(order_status({"resting": {"oid": 78}}))
        self.say("/long BTC 50")
        self.assertEqual(self.say("/confirmar"), "La orden quedó en el libro (oid 78).")
        self.assertEqual(order_count(self.ledger), 1)

    def test_rejected_order_is_not_recorded(self):
        self.hl_api.exchange.append(order_status({"error": "Insufficient margin to place order. asset=0"}))
        self.say("/long BTC 50")
        self.assertEqual(
            self.say("/confirmar"),
            "Hyperliquid rechazó la orden: Insufficient margin to place order. asset=0",
        )
        self.assertEqual(order_count(self.ledger), 0)

    def test_lost_connection_on_exchange_warns_before_retry(self):
        self.hl_api.exchange.append(TimeoutError("timed out"))
        self.say("/long BTC 50")
        self.assertEqual(self.say("/confirmar"), botmod.ORDER_UNKNOWN)
        self.assertEqual(order_count(self.ledger), 0)
        self.assertIn("No tenés ninguna orden pendiente", self.say("/confirmar"))

    def test_unreadable_keystore_asks_to_reactivate(self):
        def broken(*args):
            raise ValueError("MAC mismatch")

        self.signer.sign_l1 = broken
        self.say("/long BTC 50")
        self.assertEqual(self.say("/confirmar"), botmod.AGENT_LOCKED)
        self.assertEqual(self.hl_api.exchange_calls(), [])

    def test_orders_need_an_agent(self):
        self.say(f"/wallet {OTHER_ADDR}")
        self.assertEqual(self.say("/long BTC 50"), botmod.NEED_AGENT)

    def test_wallet_change_after_preview_blocks_the_order(self):
        self.say("/long BTC 50")
        self.say(f"/wallet {OTHER_ADDR}")
        self.assertEqual(self.say("/confirmar"), botmod.NEED_AGENT)
        self.assertEqual(self.hl_api.exchange_calls(), [])


class TestCommunityAdmin(BotCase):
    env_overrides = {"BOT_LIVE": "1"}

    def new_group(self):
        reply = self.say("/grupo_nuevo Señales AR", tg_id=ADMIN_TG)
        [group] = self.ledger.groups_for_admin(ADMIN_TG)
        return group, reply

    def test_grupo_nuevo_only_for_enabled_admins(self):
        self.assertIn("Solo los admins", self.say("/grupo_nuevo Mi grupo"))
        group, reply = self.new_group()
        self.assertEqual(group.name, "Señales AR")
        self.assertIn(f"https://t.me/OmniTraderBot?start=grp_{group.id}", reply)
        self.assertIn("Tu parte: 50%", reply)

    def test_ayuda_shows_admin_commands_only_to_admins(self):
        self.assertNotIn("/grupo_nuevo", self.say("/ayuda"))
        self.assertIn("/grupo_nuevo", self.say("/ayuda", tg_id=ADMIN_TG))

    def test_mis_grupos_and_cobro(self):
        group, _ = self.new_group()
        self.say(f"/start grp_{group.id}")
        self.assertIn("No encuentro esa comunidad", self.say(f"/cobro {group.id} {PAYOUT_ADDR}"))
        self.assertIn(PAYOUT_ADDR.lower(), self.say(f"/cobro grp_{group.id} {PAYOUT_ADDR}", tg_id=ADMIN_TG))
        listing = self.say("/mis_grupos", tg_id=ADMIN_TG)
        self.assertIn("traders: 1", listing)
        self.assertIn(f"cobro: {PAYOUT_ADDR.lower()}", listing)

    def test_liquidacion_ingests_bot_fills_and_splits_half(self):
        group, _ = self.new_group()
        self.onboard(start=f"/start grp_{group.id}")
        self.hl_api.exchange.append(filled(77))
        self.say("/long BTC 50")
        self.say("/confirmar")
        self.hl_api.info["userFillsByTime"] = [
            _fill(1, 77, builder_fee="0.01486", px="97200", sz="0.00051", time_ms=NOW * 1000),
            _fill(2, 999, builder_fee="5", time_ms=NOW * 1000),
        ]
        reply = self.say(f"/liquidacion {group.id} 2026-11", tg_id=ADMIN_TG)
        self.assertIn("Fills del bot: 1 · volumen 49.57 USD", reply)
        self.assertIn("Comisiones de builder: 0.014860 USDC", reply)
        self.assertIn("Tu parte (50%): 0.007430 USDC", reply)
        self.assertIn("Operador: 0.007430 USDC", reply)
        self.assertIn("Cálculo informativo: no se transfirió nada.", reply)
        start_ms, end_ms = month_bounds_ms(2026, 11)
        self.assertEqual(
            self.hl_api.queries("userFillsByTime"),
            [{"type": "userFillsByTime", "user": TRADER_ADDR, "startTime": start_ms, "endTime": end_ms}],
        )

    def test_liquidacion_period_and_ownership(self):
        group, _ = self.new_group()
        self.assertIn("Liquidación 2026-11", self.say(f"/liquidacion {group.id}", tg_id=ADMIN_TG))
        self.assertIn("No encuentro esa comunidad", self.say(f"/liquidacion {group.id}"))
        self.assertIn("Mes inválido", self.say(f"/liquidacion {group.id} 2026-13", tg_id=ADMIN_TG))
        self.assertIn("AAAA-MM", self.say(f"/liquidacion {group.id} noviembre", tg_id=ADMIN_TG))


class TestSafety(BotCase):
    def test_private_key_is_never_echoed_or_stored(self):
        key = "4f" * 32
        for text in (f"/wallet 0x{key}", f"mi clave es {key}"):
            with self.subTest(text=text[:12]):
                reply = self.say(text)
                self.assertEqual(reply, KEY_WARNING)
                self.assertNotIn(key, reply)
        self.assertIsNone(self.ledger.get_trader(TRADER_TG))
        self.assertEqual(self.hl_api.calls, [])

    def test_only_private_text_messages_get_replies(self):
        group_chat = private_update(1, "/start", chat_id=-100)
        group_chat["message"]["chat"]["type"] = "group"
        photo = private_update(2, "")
        del photo["message"]["text"]
        self.assertIsNone(self.bot.handle_update(group_chat))
        self.assertIsNone(self.bot.handle_update(photo))
        self.assertIsNone(self.bot.handle_update({"update_id": 3}))
        chat_id, text = self.bot.handle_update(private_update(4, "/ayuda", chat_id=555))
        self.assertEqual(chat_id, 555)
        self.assertTrue(text.startswith("Comandos:"))

    def test_unknown_input(self):
        self.assertIn("No conozco ese comando", self.say("/retirar"))
        self.assertIn("/ayuda", self.say("hola"))

    def test_hyperliquid_unreachable_or_garbled(self):
        self.say(f"/wallet {TRADER_ADDR}")
        self.hl_api.info["maxBuilderFee"] = urllib.error.URLError("sin red")
        self.assertEqual(self.say("/verificar"), botmod.HL_DOWN)
        self.hl_api.info["maxBuilderFee"] = b"<html>502</html>"
        self.assertEqual(self.say("/verificar"), botmod.HL_DOWN)

    def test_unexpected_errors_log_only_the_type(self):
        self.say(f"/wallet {TRADER_ADDR}")
        self.hl_api.info["maxBuilderFee"] = RuntimeError("detalle interno")
        with self.assertLogs("omni_builder_bot", level="ERROR") as logs:
            self.assertEqual(self.say("/verificar"), "Algo salió mal; probá de nuevo.")
        self.assertIn("RuntimeError", logs.output[0])
        self.assertNotIn("detalle interno", logs.output[0])


class TestBotConfig(unittest.TestCase):
    def test_defaults_are_testnet_and_dry_run(self):
        config = BotConfig.from_env(ENV)
        self.assertEqual(config.network, "testnet")
        self.assertFalse(config.live)
        self.assertEqual(config.builder_tenths_bps, 30)
        self.assertEqual(config.builder_address, BUILDER_ADDR.lower())
        self.assertEqual(config.username, "OmniTraderBot")
        self.assertEqual(config.db_path, "builder_bot.db")
        self.assertIsNone(config.keystore_passphrase)
        self.assertEqual(config.admin_tg_ids, frozenset({1, 2}))

    def test_fee_must_be_1_to_100_tenths_of_bp(self):
        for raw in ("0", "101", "-1", "tres"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                BotConfig.from_env({**ENV, "HYPERLIQUID_BUILDER_TENTHS_BPS": raw})
        config = BotConfig.from_env({**ENV, "HYPERLIQUID_BUILDER_TENTHS_BPS": "100"})
        self.assertEqual(config.builder_tenths_bps, 100)

    def test_required_values(self):
        for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_USERNAME", "HYPERLIQUID_BUILDER_ADDRESS", "SIGN_PAGE_URL"):
            env = {key: value for key, value in ENV.items() if key != name}
            with self.subTest(name=name), self.assertRaises(ValueError):
                BotConfig.from_env(env)

    def test_rejects_bad_values(self):
        bad = [
            ("HL_NETWORK", "devnet"),
            ("SIGN_PAGE_URL", "http://firmar.example.com/firmar.html"),
            ("SIGN_PAGE_URL", "https://firmar.example.com/firmar.html#x"),
            ("BOT_ADMIN_TG_IDS", "1,abc"),
            ("HYPERLIQUID_BUILDER_ADDRESS", "0x123"),
            ("HYPERLIQUID_BUILDER_ADDRESS", "0x" + "0" * 40),
            ("TELEGRAM_BOT_USERNAME", "mal usuario"),
        ]
        for name, value in bad:
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                BotConfig.from_env({**ENV, name: value})

    def test_live_flag_and_local_sign_page(self):
        config = BotConfig.from_env(
            {**ENV, "BOT_LIVE": "Sí", "SIGN_PAGE_URL": "http://localhost:8765/firmar.html"}
        )
        self.assertTrue(config.live)
        self.assertEqual(config.sign_page_url, "http://localhost:8765/firmar.html")

    def test_repr_hides_secrets(self):
        config = BotConfig.from_env({**ENV, "BOT_KEYSTORE_PASSPHRASE": "frase-de-prueba"})
        self.assertNotIn(FAKE_TOKEN, repr(config))
        self.assertNotIn("frase-de-prueba", repr(config))
        self.assertIn("OmniTraderBot", repr(config))


class TestTelegramApi(unittest.TestCase):
    def test_errors_never_include_the_token(self):
        def http_error(url, body, timeout):
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

        def offline(url, body, timeout):
            raise urllib.error.URLError(f"no route to {url}")

        def rejected(url, body, timeout):
            return json.dumps({"ok": False, "description": "Bad Request: chat not found"}).encode()

        for transport, expected in ((http_error, "HTTP 401"), (offline, "URLError"), (rejected, "chat not found")):
            with self.subTest(expected=expected):
                with self.assertRaises(TelegramError) as ctx:
                    TelegramApi(FAKE_TOKEN, transport).send_message(1, "hola")
                rendered = "".join(traceback.format_exception(ctx.exception))
                self.assertIn(expected, rendered)
                self.assertNotIn(FAKE_TOKEN, rendered)
        self.assertNotIn(FAKE_TOKEN, repr(TelegramApi(FAKE_TOKEN)))

    def test_send_message_truncates_and_disables_previews(self):
        sent = []

        def transport(url, body, timeout):
            sent.append((url, json.loads(body)))
            return b'{"ok": true, "result": {"message_id": 1}}'

        TelegramApi(FAKE_TOKEN, transport).send_message(7, "x" * 5000)
        url, payload = sent[0]
        self.assertTrue(url.endswith("/sendMessage"))
        self.assertEqual(len(payload["text"]), 4096)
        self.assertEqual(payload["link_preview_options"], {"is_disabled": True})

    def test_get_updates_offset_and_timeout(self):
        sent = []

        def transport(url, body, timeout):
            sent.append((json.loads(body), timeout))
            return b'{"ok": true, "result": []}'

        api = TelegramApi(FAKE_TOKEN, transport)
        self.assertEqual(api.get_updates(None), [])
        api.get_updates(42)
        self.assertEqual(sent[0][0], {"timeout": 25, "allowed_updates": ["message"]})
        self.assertEqual(sent[1][0]["offset"], 42)
        self.assertGreater(sent[0][1], 25)


class ScriptedTelegram:
    def __init__(self, script):
        self.script = list(script)
        self.offsets = []
        self.sent = []

    def get_updates(self, offset, timeout=25):
        self.offsets.append(offset)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


class TestRunPolling(BotCase):
    def test_offsets_backoff_and_survival(self):
        broken = private_update(11, "/ayuda")
        broken["message"]["chat"]["id"] = "no-numerico"
        api = ScriptedTelegram(
            [
                TelegramError("caída"),
                TelegramError("caída"),
                [private_update(10, "/start"), broken, private_update(12, "/ayuda")],
                [],
            ]
        )
        sleeps = []
        with self.assertLogs("omni_builder_bot", level="WARNING"):
            run_polling(api, self.bot, sleep=sleeps.append, max_cycles=4)
        self.assertEqual(api.offsets, [None, None, None, 13])
        self.assertEqual(sleeps, [1.0, 2.0])
        self.assertEqual([chat_id for chat_id, _ in api.sent], [TRADER_TG, TRADER_TG])


class TestMain(unittest.TestCase):
    def run_main(self, env):
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stderr(stderr):
            code = botmod.main()
        return code, stderr.getvalue()

    def test_invalid_config_exits_2_without_secrets(self):
        code, err = self.run_main({**ENV, "HYPERLIQUID_BUILDER_TENTHS_BPS": "500"})
        self.assertEqual(code, 2)
        self.assertIn("Configuración inválida", err)
        self.assertNotIn(FAKE_TOKEN, err)

    def test_live_mode_requires_keystore_passphrase(self):
        code, err = self.run_main({**ENV, "BOT_LIVE": "1"})
        self.assertEqual(code, 2)
        self.assertIn("BOT_KEYSTORE_PASSPHRASE", err)


if __name__ == "__main__":
    unittest.main()
