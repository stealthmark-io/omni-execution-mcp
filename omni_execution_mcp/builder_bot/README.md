# Bot de builder codes para comunidades (Telegram + Hyperliquid)

Bot en español para que una comunidad de trading opere perps de Hyperliquid desde Telegram con tu builder code. Cada orden lleva tu builder fee; cada trader queda atribuido a la comunidad que lo invitó y el bot calcula la parte del admin. Las órdenes van directo a `/info` y `/exchange` de Hyperliquid: no hay pre-flight x402 en el camino del trade.

Por defecto corre en **testnet** y en **simulación**: arma y muestra la orden, pero no firma ni envía nada.

## Probarlo en local

1. Creá el bot con @BotFather y guardá el token.
2. Copiá `.env.example` a `.env` (nunca a git) y completá `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `HYPERLIQUID_BUILDER_ADDRESS` (tu dirección pública de builder), `BOT_ADMIN_TG_IDS` (tu ID de Telegram) y `SIGN_PAGE_URL=http://localhost:8765/firmar.html`.
3. Serví la página de firma. Las wallets no se inyectan en `file://`, por eso va por HTTP local:
   ```bash
   python3 -m http.server 8765 --bind 127.0.0.1 --directory omni_execution_mcp/builder_bot/static
   ```
4. Desde `omni-execution-mcp/`, arrancá el bot:
   ```bash
   set -a; . ./.env; set +a; python3 -m omni_execution_mcp.builder_bot.bot
   ```

Para firmar y enviar órdenes en testnet: `pip install '.[bot]'` (hyperliquid-python-sdk 0.24), `BOT_KEYSTORE_PASSPHRASE` con una frase larga y `BOT_LIVE=1`.

## Flujo

- **Trader:** entra por `https://t.me/<bot>?start=grp_<id>` → `/wallet 0x…` (solo la dirección pública) → `/activar` (firma `approveBuilderFee` y `approveAgent` en `firmar.html`) → `/verificar` → `/long BTC 25` → `/confirmar` dentro de los 60 s.
- **Admin de comunidad:** `/grupo_nuevo NOMBRE` → comparte el link → `/cobro ID 0x…` → `/liquidacion ID AAAA-MM`.
- La atribución es por primer contacto: un trader que ya entró por una comunidad no cambia de comunidad con otro link.

## Reglas de Hyperliquid que importan

- Tope del builder fee en perps: 0.1% (`HYPERLIQUID_BUILDER_TENTHS_BPS` hasta 100; 30 = 0.03%). Orden mínima: 10 USD.
- La dirección de builder necesita al menos 100 USDC de account value en perps; si no, Hyperliquid rechaza las órdenes con builder.
- Hyperliquid acumula los builder fees como rewards de referidos de la dirección de builder (`{"type": "referral"}` en `/info`) y se cobran con el claim de rewards de Hyperliquid. El bot no mueve fondos.
- El agente usa siempre el nombre `omnibot`: aprobar el mismo nombre reemplaza al agente anterior, así que repetir `/activar` no gasta slots (1 agente sin nombre y 3 con nombre por cuenta). Cada `/activar` genera una key nueva: nunca se reusa una dirección de agente.
- No usamos `valid_until`. Hyperliquid lo lee como sufijo del nombre del agente (`omnibot valid_until <ms>`) y no verificamos cómo empareja nombres con sufijo: con un nombre fijo, el reemplazo está garantizado. Para revocar, el trader aprueba otro agente con el mismo nombre desde Hyperliquid.

## Seguridad

- El bot nunca pide claves privadas. Si alguien pega algo que parece una, no lo guarda, no lo repite y le avisa que la considere comprometida.
- La base (`builder_bot.db`, ignorada por git) guarda las keys de los agentes cifradas con `BOT_KEYSTORE_PASSPHRASE`. Un agente opera pero no retira ni transfiere. Si perdés la frase, cada trader vuelve a `/activar`.
- `firmar.html` tiene CSP estricta: solo habla con las APIs de Hyperliquid y no carga nada externo.

## Liquidación

`/liquidacion` baja los fills del mes de cada trader de la comunidad (`userFillsByTime`), se queda solo con las órdenes que mandó el bot y suma su `builderFee` en USDC. El reparto por defecto es 50% admin y 50% operador. Es un cálculo informativo: no paga nada.

## Antes de producción (cada punto necesita un "sí" explícito)

- Hospedar `firmar.html` en HTTPS y apuntar `SIGN_PAGE_URL` ahí.
- Pasar a mainnet (`HL_NETWORK=mainnet`).
- Pagar a los admins: hoy solo se calcula.
- Regulatorio: en Argentina, operar activos virtuales por cuenta de terceros puede requerir inscripción como PSAV ante la CNV, y otros países de LATAM tienen reglas parecidas. Esto no es asesoramiento legal: validalo con un abogado antes de cobrar en mainnet.

## Limitaciones conocidas

- Si una orden en vivo entra pero se pierde la respuesta de Hyperliquid, no queda registrada y su builder fee no se atribuye a la comunidad. El bot avisa al trader que revise sus posiciones antes de reintentar.
- Las órdenes pendientes de confirmar viven en memoria: si el bot se reinicia, hay que armarlas de nuevo.
- El detector de claves también salta con cualquier texto de 64 caracteres hex, como un hash de transacción (falso positivo seguro).
- Algunos errores de librerías de terceros pueden llegar en inglés.
- Si Telegram responde con un error HTTP, el log muestra solo el código, no la descripción.
- El recorte a 4096 caracteres cuenta code points; Telegram cuenta UTF-16, así que un mensaje con muchos emojis podría quedar largo.
