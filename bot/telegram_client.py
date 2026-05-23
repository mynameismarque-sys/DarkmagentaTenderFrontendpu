"""
Cliente Telethon — sesión PERMANENTE para @FFPROXYCHEAT_BOT.

DISEÑO DE AISLAMIENTO (v2):
  Telethon corre en un thread DEDICADO con su propio event loop (_tg_loop).
  El event loop de Discord NUNCA es bloqueado por operaciones de red de Telethon.
  Las llamadas async públicas (cmd_gen, cmd_key) usan asyncio.wrap_future para
  bridgear entre el loop de Discord y _tg_loop sin bloquear a ninguno.

  Esto resuelve el error "interacción expirada" que ocurría porque Telethon
  procesaba InvalidBufferError + reconexión en el mismo loop que Discord,
  impidiendo que el defer llegara a Discord dentro del límite de 3 segundos.
"""
import asyncio
import logging
import os
import threading
import time

import socks
from telethon import TelegramClient
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyInvalidError,
    AuthKeyPermEmptyError,
    AuthKeyUnregisteredError,
    FloodWaitError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
    UserDeactivatedBanError,
)
from telethon.errors.common import AuthKeyNotFound
from telethon.sessions import StringSession

log = logging.getLogger("bot.telegram")

# True cuando corre en el deployment de producción de Replit.
IS_PRODUCTION: bool = bool(os.environ.get("REPLIT_DEPLOYMENT"))

BOT_USERNAME   = "@FFPROXYCHEAT_BOT"
STEP_TIMEOUT   = 20    # segundos por paso de conversación
PING_INTERVAL  = 60    # ping periódico para detectar auth errors
CHECK_INTERVAL = 10    # cada 10s el keepalive verifica is_connected()
DUP_RETRY_SECS = 300   # segundos entre reintentos cuando prod tiene la sesión

# ─── Thread y loop dedicado para Telethon ────────────────────────────────────
_tg_loop:   asyncio.AbstractEventLoop | None = None
_tg_thread: threading.Thread | None          = None

# Locks asyncio — creados dentro del thread de Telethon (en su loop)
_lock:           asyncio.Lock | None = None
_reconnect_lock: asyncio.Lock | None = None
_keepalive_task: asyncio.Task | None = None
_connect_task:   asyncio.Task | None = None

# ─── Estado global ────────────────────────────────────────────────────────────
_client: TelegramClient | None = None

# threading.Event: seguro entre threads (Discord lee, Telethon escribe)
_ready = threading.Event()

_prod_has_session: bool  = False
_next_dup_retry:   float = 0.0

_SESSION_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "telegram_session.txt"
)

_BAD_AUTH_ERRORS = (
    AuthKeyDuplicatedError,
    AuthKeyInvalidError,
    AuthKeyNotFound,
    AuthKeyPermEmptyError,
    AuthKeyUnregisteredError,
    UserDeactivatedBanError,
)


# ─────────────────────────────────────────────────────────────────────────────
# Thread management
# ─────────────────────────────────────────────────────────────────────────────

def _run_tg_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Función que corre en el thread de Telethon. Crea los locks en el loop correcto."""
    global _lock, _reconnect_lock
    asyncio.set_event_loop(loop)
    _lock           = asyncio.Lock()
    _reconnect_lock = asyncio.Lock()
    loop.run_forever()


def _ensure_tg_loop() -> asyncio.AbstractEventLoop:
    """Crea e inicia el thread/loop de Telethon si no existe."""
    global _tg_loop, _tg_thread
    if _tg_loop is not None and _tg_thread is not None and _tg_thread.is_alive():
        return _tg_loop

    _tg_loop   = asyncio.new_event_loop()
    _tg_thread = threading.Thread(
        target=_run_tg_loop,
        args=(_tg_loop,),
        name="telethon-loop",
        daemon=True,
    )
    _tg_thread.start()

    # Esperar a que los locks estén creados (máx 1s)
    for _ in range(100):
        if _lock is not None:
            break
        time.sleep(0.01)

    return _tg_loop


def _submit(coro) -> "asyncio.Future":
    """
    Envía una coroutine al loop de Telethon y devuelve un asyncio.Future
    compatible con el loop de Discord (via wrap_future).
    """
    loop = _ensure_tg_loop()
    return asyncio.wrap_future(
        asyncio.run_coroutine_threadsafe(coro, loop)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Lectura y escritura de sesión
# ─────────────────────────────────────────────────────────────────────────────

def _read_best_session() -> str:
    import base64 as _b64

    IS_PRODUCTION = bool(os.environ.get("REPLIT_DEPLOYMENT"))

    def _clean(s: str) -> str:
        return s.strip().replace("\n", "").replace("\r", "").replace(" ", "")

    def _is_valid(s: str) -> bool:
        if not s:
            return False
        b64 = s[1:]
        if len(b64) % 4 != 0:
            return False
        try:
            _b64.urlsafe_b64decode(b64)
            return True
        except Exception:
            return False

    def _fix_padding(s: str) -> str:
        b64 = s[1:]
        rem = len(b64) % 4
        return (s + "=" * (4 - rem)) if rem else s

    def _try_load(s: str, label: str) -> str | None:
        s = _clean(s)
        if _is_valid(s):
            return s
        fixed = _fix_padding(s)
        if _is_valid(fixed):
            log.info("Telethon: padding corregido en %s (largo %d→%d).", label, len(s), len(fixed))
            return fixed
        return None

    if IS_PRODUCTION:
        log.info("Telethon: modo PRODUCCIÓN — usando TELEGRAM_SESSION env var.")
        env_str = os.environ.get("TELEGRAM_SESSION", "")
        result = _try_load(env_str, "TELEGRAM_SESSION (prod)")
        if result:
            return result
        log.error("Telethon: TELEGRAM_SESSION de producción inválida.")
        return _clean(env_str)

    if os.path.exists(_SESSION_FILE):
        file_str = open(_SESSION_FILE, encoding="utf-8").read()
        result = _try_load(file_str, "archivo telegram_session.txt")
        if result:
            return result
        log.warning("Telethon: archivo de sesión inválido — usando TELEGRAM_SESSION secret.")

    env_str = os.environ.get("TELEGRAM_SESSION", "")
    result = _try_load(env_str, "TELEGRAM_SESSION (dev secret)")
    if result:
        return result

    log.error("Telethon: no hay sesión válida. Ejecutá: python tools/gen_telegram_session.py")
    return _clean(env_str)


def _save_session(client: TelegramClient) -> None:
    try:
        session_str = client.session.save()
        if session_str:
            with open(_SESSION_FILE, "w", encoding="utf-8") as f:
                f.write(session_str)
            log.debug("Telethon: sesión guardada (largo=%d).", len(session_str))
    except Exception as e:
        log.warning("Telethon: no se pudo guardar sesión: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Proxy
# ─────────────────────────────────────────────────────────────────────────────

def _build_proxy() -> tuple | None:
    ip   = os.environ.get("PROXY_IP",   "").strip()
    port = os.environ.get("PROXY_PORT", "").strip()
    user = os.environ.get("PROXY_USER", "").strip()
    pwd  = os.environ.get("PROXY_PASS", "").strip()

    if not all([ip, port, user, pwd]):
        missing = [k for k, v in {"PROXY_IP": ip, "PROXY_PORT": port,
                                   "PROXY_USER": user, "PROXY_PASS": pwd}.items() if not v]
        log.warning("Telethon: proxy no configurado (faltan secrets: %s) — conexión directa.", missing)
        return None

    proxy_type_str = os.environ.get("PROXY_TYPE", "HTTP").strip().upper()
    proxy_type = socks.SOCKS5 if proxy_type_str == "SOCKS5" else socks.HTTP

    try:
        port_int = int(port)
    except ValueError:
        log.error("Telethon: PROXY_PORT inválido (%r) — conexión directa.", port)
        return None

    type_name = "SOCKS5" if proxy_type == socks.SOCKS5 else "HTTP"
    log.info("Telethon: proxy %s → %s:%d (user=%s).", type_name, ip, port_int, user)
    return (proxy_type, ip, port_int, True, user, pwd)


# ─────────────────────────────────────────────────────────────────────────────
# Creación y conexión del cliente (corren en _tg_loop)
# ─────────────────────────────────────────────────────────────────────────────

def _make_client() -> TelegramClient:
    api_id      = int(os.environ["TELEGRAM_API_ID"])
    api_hash    = os.environ["TELEGRAM_API_HASH"]
    session_str = _read_best_session()
    proxy       = _build_proxy()
    conn_desc   = "con proxy" if proxy else "conexión directa"
    log.info("Telethon: creando cliente (sesión largo=%d, %s).", len(session_str), conn_desc)
    return TelegramClient(
        StringSession(session_str),
        api_id,
        api_hash,
        proxy=proxy,
        auto_reconnect=True,
        connection_retries=10,
        retry_delay=3,
    )


async def _connect_and_auth(client: TelegramClient) -> bool:
    """Corre en _tg_loop. Conecta y verifica autorización."""
    TIMEOUT = 60  # 60s — producción puede tardar más que dev en llegar a Telegram

    if not client.is_connected():
        try:
            await asyncio.wait_for(client.connect(), timeout=TIMEOUT)
        except _BAD_AUTH_ERRORS:
            raise
        except asyncio.TimeoutError:
            raise ConnectionError("connect() timeout (60s)")
        except Exception as e:
            raise ConnectionError(f"connect() error: {type(e).__name__}: {e}") from e

    try:
        authorized = await asyncio.wait_for(client.is_user_authorized(), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        raise ConnectionError("is_user_authorized() timeout")

    if not authorized:
        log.error("Telethon: sesión no autorizada. Regenerá TELEGRAM_SESSION.")
        return False

    try:
        me = await asyncio.wait_for(client.get_me(), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        raise ConnectionError("get_me() timeout")

    _save_session(client)
    _ready.set()
    log.info("✅ Telethon conectado como %s (id=%s)", me.first_name, me.id)
    return True


async def _do_reconnect() -> bool:
    """Corre en _tg_loop. Reconecta reutilizando el cliente existente."""
    global _client, _prod_has_session, _next_dup_retry
    _ready.clear()

    if _client is None:
        _client = _make_client()

    try:
        ok = await _connect_and_auth(_client)
        if ok:
            _prod_has_session = False
            _next_dup_retry   = 0.0
        return ok
    except AuthKeyDuplicatedError:
        _prod_has_session = True
        _next_dup_retry   = time.monotonic() + DUP_RETRY_SECS
        log.warning(
            "Telethon: AuthKeyDuplicatedError — producción tiene la sesión activa. "
            "El keepalive reintentará en %ds.", DUP_RETRY_SECS,
        )
        raise
    except _BAD_AUTH_ERRORS as e:
        log.warning("Telethon: %s — recreando cliente desde sesión guardada...", type(e).__name__)

    try:
        await _client.disconnect()
    except Exception:
        pass
    _client = _make_client()

    try:
        ok = await _connect_and_auth(_client)
        if ok:
            _prod_has_session = False
        return ok
    except AuthKeyDuplicatedError:
        _prod_has_session = True
        _next_dup_retry   = time.monotonic() + DUP_RETRY_SECS
        log.warning("Telethon: AuthKeyDuplicatedError también con cliente nuevo — producción activa.")
        raise
    except _BAD_AUTH_ERRORS as e:
        log.error(
            "Telethon: %s incluso con sesión guardada — "
            "necesitás regenerar TELEGRAM_SESSION: python tools/gen_telegram_session.py",
            type(e).__name__,
        )
        _client = None
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Loop de keepalive — corre en _tg_loop, nunca toca el loop de Discord
# ─────────────────────────────────────────────────────────────────────────────

async def _keepalive_loop() -> None:
    log.info("Telethon keepalive iniciado (check=%ds, ping=%ds).", CHECK_INTERVAL, PING_INTERVAL)
    last_ping  = asyncio.get_event_loop().time()
    auth_delay = 60

    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        now = asyncio.get_event_loop().time()

        if _prod_has_session:
            if time.monotonic() < _next_dup_retry:
                continue
            log.info("Telethon keepalive: reintentando conexión (producción quizás se detuvo)...")
            async with _reconnect_lock:
                try:
                    ok = await _do_reconnect()
                    if ok:
                        log.info("✅ Telethon keepalive: reconectado OK — producción se detuvo.")
                        last_ping  = now
                        auth_delay = 60
                except AuthKeyDuplicatedError:
                    log.info("Telethon keepalive: producción sigue activa — reintentando en %ds.", DUP_RETRY_SECS)
                except Exception as _dup_exc:
                    # Error distinto a AuthKeyDuplicatedError (p.ej. sesión inválida,
                    # no autorizada, etc.). Producción ya no tiene la sesión → reset
                    # para que el bot intente reconectarse normalmente en el próximo ciclo.
                    log.warning(
                        "Telethon keepalive: error no-dup en reintento (%s) — "
                        "reseteando _prod_has_session para reintentar normalmente.",
                        type(_dup_exc).__name__,
                    )
                    _prod_has_session = False
                    _next_dup_retry   = 0.0
            continue

        connected = False
        if _client is not None:
            try:
                connected = _client.is_connected()
            except Exception:
                connected = False

        if not connected:
            _ready.clear()
            log.warning("Telethon keepalive: sin conexión — reconectando...")
            async with _reconnect_lock:
                if is_ready():
                    auth_delay = 60
                    continue
                try:
                    ok = await _do_reconnect()
                    if ok:
                        log.info("Telethon keepalive: reconectado OK.")
                        last_ping  = now
                        auth_delay = 60
                    else:
                        log.error("Telethon: sesión inválida. Reintentando en 120s.")
                        await asyncio.sleep(120)
                except AuthKeyDuplicatedError:
                    pass
                except _BAD_AUTH_ERRORS as e:
                    log.error("Telethon keepalive: %s — esperando %ds.", type(e).__name__, auth_delay)
                    await asyncio.sleep(auth_delay)
                    auth_delay = min(auth_delay * 2, 600)
                except FloodWaitError as e:
                    log.warning("Telethon keepalive: FloodWait %ds.", e.seconds)
                    await asyncio.sleep(e.seconds + 5)
                except Exception:
                    log.exception("Telethon keepalive: error — reintentando en 15s.")
                    await asyncio.sleep(15)
            continue

        if now - last_ping >= PING_INTERVAL:
            try:
                await asyncio.wait_for(_client.get_me(), timeout=15)
                _save_session(_client)
                last_ping  = now
                auth_delay = 60
                log.debug("Telethon ping OK — sesión guardada.")
            except AuthKeyDuplicatedError:
                log.warning("Telethon ping: AuthKeyDuplicatedError — otra instancia tomó la sesión.")
                _ready.clear()
                try:
                    if _client and _client.is_connected():
                        await _client.disconnect()
                except Exception:
                    pass
            except _BAD_AUTH_ERRORS as e:
                log.warning("Telethon ping: %s — forzando reconexión.", type(e).__name__)
                _ready.clear()
                try:
                    if _client and _client.is_connected():
                        await _client.disconnect()
                except Exception:
                    pass
            except asyncio.TimeoutError:
                log.warning("Telethon ping: timeout — Telethon reconectará solo.")
            except Exception as e:
                log.warning("Telethon ping: %s.", e)


# ─────────────────────────────────────────────────────────────────────────────
# API pública
# ─────────────────────────────────────────────────────────────────────────────

async def _connect_async() -> None:
    """Corre en _tg_loop. Inicia cliente y keepalive."""
    global _keepalive_task

    if not os.environ.get("TELEGRAM_API_ID"):
        log.warning("TELEGRAM_API_ID no configurado — /gen y /key desactivados.")
        return

    try:
        ok = await _do_reconnect()
        if not ok:
            log.warning("Telethon: sesión no autorizada. Verificá TELEGRAM_SESSION.")
    except AuthKeyDuplicatedError:
        log.warning(
            "Telethon: producción tiene la sesión. El keepalive reintentará cada %ds.", DUP_RETRY_SECS
        )
    except _BAD_AUTH_ERRORS as e:
        log.error("Telethon: %s — el keepalive reintentará automáticamente.", type(e).__name__)
    except Exception:
        log.exception("Telethon: error en conexión inicial — el keepalive reintentará.")

    if _keepalive_task is None or _keepalive_task.done():
        _keepalive_task = asyncio.create_task(_keepalive_loop(), name="telethon-keepalive")


def schedule_connect() -> None:
    """
    Inicia Telethon en su thread dedicado. Llamar desde on_ready de Discord.
    No bloquea el loop de Discord.
    """
    loop = _ensure_tg_loop()
    asyncio.run_coroutine_threadsafe(_connect_async(), loop)


async def disconnect() -> None:
    """Cierre limpio. Llamar desde el contexto de Discord."""
    global _client, _keepalive_task, _tg_loop, _tg_thread

    tg_loop = _tg_loop
    if tg_loop is None:
        return

    async def _do_disconnect():
        global _client, _keepalive_task
        if _keepalive_task and not _keepalive_task.done():
            _keepalive_task.cancel()
            try:
                await _keepalive_task
            except asyncio.CancelledError:
                pass
            _keepalive_task = None
        _ready.clear()
        if _client:
            try:
                if _client.is_connected():
                    await _client.disconnect()
            except Exception:
                pass
            log.info("Telethon desconectado.")
        _client = None

    await _submit(_do_disconnect())


def is_ready() -> bool:
    """True si hay cliente conectado y autenticado. Thread-safe."""
    if not _ready.is_set():
        return False
    if _client is None:
        return False
    try:
        return _client.is_connected()
    except Exception:
        return False


def prod_has_session() -> bool:
    """True si producción tiene la sesión de Telegram activa (AuthKeyDuplicatedError).
    Usado por el bot de desarrollo para ceder interacciones al bot de producción.
    Thread-safe.
    """
    return _prod_has_session


# ─────────────────────────────────────────────────────────────────────────────
# _ensure_ready — corre en _tg_loop
# ─────────────────────────────────────────────────────────────────────────────

_MSG_PROD_ACTIVA = (
    "⚠️ Telegram está siendo usado por la instancia de producción "
    "(los clientes pueden seguir usando /gen y /key normalmente). "
    "Si necesitás probar desde desarrollo, pausá el deployment primero."
)
_MSG_NO_CONEXION = (
    "⚠️ Sin conexión a Telegram en este momento. "
    "El bot reintenta automáticamente — intentá de nuevo en unos segundos."
)


async def _ensure_ready() -> None:
    """Corre en _tg_loop. Garantiza conexión activa antes de ejecutar un comando."""
    if is_ready():
        return

    # Solo el bot de DESARROLLO cede cuando producción tiene la sesión activa.
    # En producción, _prod_has_session indica conflicto temporal con dev —
    # producción debe seguir intentando conectarse, no bloquearse.
    if _prod_has_session and not IS_PRODUCTION:
        raise RuntimeError(_MSG_PROD_ACTIVA)

    log.info("Telethon: comando sin conexión activa — reconectando...")

    async with _reconnect_lock:
        if is_ready():
            return

        if _prod_has_session and not IS_PRODUCTION:
            raise RuntimeError(_MSG_PROD_ACTIVA)

        for intento in range(1, 4):
            try:
                ok = await _do_reconnect()
                if ok:
                    log.info("Telethon: reconexión activa OK (intento %d/3).", intento)
                    return
                raise RuntimeError(
                    "❌ Sesión de Telegram no autorizada. "
                    "Regenerá TELEGRAM_SESSION: python tools/gen_telegram_session.py"
                )
            except RuntimeError:
                raise
            except AuthKeyDuplicatedError:
                if not IS_PRODUCTION:
                    raise RuntimeError(_MSG_PROD_ACTIVA)
                # En producción: dev también está corriendo → esperar y reintentar
                log.warning(
                    "Telethon (prod): AuthKeyDuplicated en _ensure_ready (intento %d/3) "
                    "— dev bot también activo, reintentando en 5s...", intento
                )
                await asyncio.sleep(5)
            except _BAD_AUTH_ERRORS as e:
                raise RuntimeError(
                    f"❌ Sesión de Telegram inválida ({type(e).__name__}). "
                    "Regenerá TELEGRAM_SESSION: python tools/gen_telegram_session.py"
                )
            except FloodWaitError as e:
                if intento < 3:
                    await asyncio.sleep(min(e.seconds, 30))
                else:
                    raise RuntimeError(f"⏳ Telegram pidió esperar {e.seconds}s. Intentá de nuevo.")
            except Exception as e:
                log.warning("Telethon: reconexión intento %d/3 falló: %s", intento, e)
                if intento < 3:
                    await asyncio.sleep(5)

        if not is_ready():
            raise RuntimeError(_MSG_NO_CONEXION)


# ─────────────────────────────────────────────────────────────────────────────
# Comandos con retry automático — corren en _tg_loop
# ─────────────────────────────────────────────────────────────────────────────

async def _run_with_retry(coro_fn, label: str):
    for attempt in range(1, 4):  # 3 intentos
        await _ensure_ready()
        try:
            return await coro_fn()
        except (ValueError, RuntimeError):
            raise
        except FloodWaitError as e:
            raise RuntimeError(
                f"⏳ Telegram pidió esperar {e.seconds}s. Intentá de nuevo."
            ) from e
        except Exception as exc:
            _ready.clear()
            if attempt < 3:
                log.warning("Telethon: %s falló (intento %d/3): %s — reintentando...", label, attempt, exc)
                await asyncio.sleep(2)
            else:
                log.error("Telethon: %s falló definitivamente: %s", label, exc)
                raise RuntimeError(f"❌ Error al comunicar con Telegram: {exc}") from exc


async def _cmd_gen_impl(key: str, dias: int) -> str:
    """
    Implementación real de cmd_gen. Corre en _tg_loop.
    Usa polling manual (igual que _cmd_key_impl) para evitar timeouts
    de la API conversation() cuando el bot tarda en responder al /start.
    """
    async def _poll_gen(after_id: int, timeout: int = 20):
        """Polling simple: espera mensaje IN del bot con id > after_id."""
        log.info("[GEN-POLL] Esperando msg entrante after_id=%d timeout=%ds", after_id, timeout)
        for iteration in range(timeout):
            await asyncio.sleep(1)
            msgs = await _client.get_messages(BOT_USERNAME, limit=5)
            for msg in msgs:
                if msg.id <= after_id:
                    break
                if msg.out:
                    continue
                t = (msg.text or msg.message or "").strip()
                if t:
                    log.info("[GEN-POLL] ✓ iter=%d id=%d text=%r", iteration, msg.id, t[:80])
                    return msg, t
        raise RuntimeError(
            f"⏱️ El bot de Telegram no respondió al /gen en {timeout}s. Intentá de nuevo."
        )

    async def _do():
        async with _lock:
            prev = await _client.get_messages(BOT_USERNAME, limit=1)
            baseline = prev[0].id if prev else 0

            # Enviar /start — no bloqueamos esperando el welcome (max 5s)
            await _client.send_message(BOT_USERNAME, "/start")
            last = baseline
            try:
                w_msg, _ = await _poll_gen(baseline, timeout=5)
                last = w_msg.id
                log.info("[GEN-FLOW] /start welcome OK (id=%d)", last)
            except RuntimeError:
                try:
                    fresh = await _client.get_messages(BOT_USERNAME, limit=1)
                    last = fresh[0].id if fresh else baseline
                except Exception:
                    pass
                log.warning("[GEN-FLOW] /start sin respuesta en 5s — continuando con id=%d", last)

            # Enviar /gen y esperar respuesta
            await _client.send_message(BOT_USERNAME, f"/gen {key} {dias}")
            resp_msg, resp_text = await _poll_gen(last, timeout=20)
            log.info("[GEN-FLOW] respuesta /gen (1ra): %r", resp_text[:80])

            # El bot puede enviar un mensaje intermedio como "** Generating key...**"
            # antes de la respuesta real. Seguimos esperando si detectamos eso.
            _INTERMEDIATE_KW = (
                "generating key", "generating...", "generating key...",
                "generando", "please wait", "espere", "procesando",
            )
            _intentos_extra = 0
            while any(kw in resp_text.lower() for kw in _INTERMEDIATE_KW) and _intentos_extra < 3:
                _intentos_extra += 1
                log.info(
                    "[GEN-FLOW] respuesta intermedia ('%s') — esperando respuesta real (intento %d/3)...",
                    resp_text[:60], _intentos_extra,
                )
                try:
                    resp_msg, resp_text = await _poll_gen(resp_msg.id, timeout=30)
                    log.info("[GEN-FLOW] respuesta /gen (%d): %r", _intentos_extra + 1, resp_text[:80])
                except RuntimeError:
                    break

            # Verificar que el bot confirmó el registro exitosamente
            _SUCCESS_KW = ("generated successfully", "key generated", "generado exitosamente")
            if not any(kw in resp_text.lower() for kw in _SUCCESS_KW):
                log.error("[GEN-FLOW] Respuesta inesperada del bot al /gen — key puede no estar registrada: %r", resp_text[:200])
                raise Exception(f"Respuesta inesperada del bot al /gen: {resp_text}")

            return resp_text

    return await _run_with_retry(_do, "cmd_gen")


async def _cmd_key_impl(key: str, ip: str) -> str:
    """
    Implementación real de cmd_key. Corre en _tg_loop.

    Diseño: NO usa conversation() de Telethon — esa API se confunde cuando el
    bot envía múltiples mensajes en respuesta a uno solo (ej: "Checking key..."
    seguido de "Key Verified Successfully!").
    En cambio, usamos send_message() directo + polling por ID, igual que el
    bloque de click de confirmación que ya funcionaba.
    """
    _ERROR_KEYWORDS   = (
        "invalid", "error", "failed", "inválid", "incorrect",
        "not found", "network", "wrong", "expired", "no encontr",
    )
    _CONFIRM_KEYWORDS = (
        "proceed", "confirm", "want to", "desea", "continuar",
        "confirmar", "proceder", "do you",
    )

    async def _poll_new_msg(after_id: int, skip: str = "", timeout: int = 20):
        """
        Espera hasta `timeout` segundos un mensaje ENTRANTE del bot con id > after_id
        cuyo texto no contenga `skip` (case-insensitive).
        Usa msg.out=False para filtrar nuestros propios mensajes enviados.
        Devuelve (message_obj, text) o lanza RuntimeError si no llega.
        """
        log.info(
            "[KEY-POLL] Esperando msg entrante after_id=%d skip=%r timeout=%ds",
            after_id, skip, timeout,
        )
        for iteration in range(timeout):
            await asyncio.sleep(1)
            msgs = await _client.get_messages(BOT_USERNAME, limit=5)
            for msg in msgs:
                direction = "OUT" if msg.out else "IN"
                preview = (msg.text or msg.message or "")[:60].replace("\n", "⏎")
                log.info(
                    "[KEY-POLL] iter=%d id=%d %s text=%r",
                    iteration, msg.id, direction, preview,
                )
                if msg.id <= after_id:
                    log.info("[KEY-POLL] → id<=%d, rompiendo iteración", after_id)
                    break
                if msg.out:
                    log.info("[KEY-POLL] → propio (out), salteando")
                    continue
                t = (msg.text or msg.message or "").strip()
                if not t:
                    log.info("[KEY-POLL] → sin texto, salteando")
                    continue
                if skip and skip.lower() in t.lower():
                    log.info("[KEY-POLL] → contiene skip=%r, salteando", skip)
                    continue
                log.info("[KEY-POLL] ✓ msg aceptado id=%d text=%r", msg.id, t[:80])
                return msg, t
        raise RuntimeError(
            f"⏱️ El bot de Telegram no respondió en {timeout}s. Intentá de nuevo."
        )

    def _bot_in_ip_state(text: str) -> bool:
        """
        Detecta si el bot RECHAZÓ nuestra key tratándola como una IP inválida,
        lo que significa que el bot estaba en estado 'esperando IP'.

        IMPORTANTE: "Key Verified Successfully! Please send your new IP address"
        también menciona "IP address" pero NO es este estado — es el paso siguiente
        al éxito. Solo se detecta cuando hay un rechazo explícito de IP inválida.
        """
        t = text.lower()
        return "invalid ip" in t or ("please enter" in t and "valid ip" in t)

    async def _send_ip_and_confirm(after_id: int, label: str) -> tuple:
        """
        Envía `ip` al bot, espera respuesta, y si pide confirmación hace click[0].
        Devuelve (last_id, result_text).
        """
        await _client.send_message(BOT_USERNAME, ip)
        conf_msg, conf_text = await _poll_new_msg(after_id, timeout=15)
        log.info("[%s] Respuesta a IP: '%s'", label, conf_text[:80])
        last = conf_msg.id

        if any(kw in conf_text.lower() for kw in _ERROR_KEYWORDS):
            raise ValueError(conf_text)

        if any(kw in conf_text.lower() for kw in _CONFIRM_KEYWORDS):
            try:
                await conf_msg.click(0)
                log.info("[%s] Click en botón inline [0] OK", label)
                res_msg, conf_text = await _poll_new_msg(last, timeout=15)
                last = res_msg.id
                log.info("[%s] Respuesta tras click: '%s'", label, conf_text[:80])
            except Exception as exc:
                log.warning("[%s] Click en botón falló: %s", label, exc)

        if any(kw in conf_text.lower() for kw in _ERROR_KEYWORDS):
            raise ValueError(conf_text)

        return last, conf_text

    async def _fresh_start_key_flow() -> tuple:
        """
        Hace el flujo completo desde cero: baseline → /start → key → devuelve
        (last_id, key_text).  Lanza ValueError si la key es inválida.
        Lanza RuntimeError si el bot sigue en estado 'esperando IP'.

        El welcome de /start se espera máximo 5s — si no llega, se continúa
        de todas formas para no bloquear el flujo completo por un bot lento.
        """
        prev = await _client.get_messages(BOT_USERNAME, limit=1)
        baseline = prev[0].id if prev else 0

        await _client.send_message(BOT_USERNAME, "/start")

        # Intentar welcome hasta 5s (no bloqueamos 15s si el bot tarda)
        last = baseline
        try:
            w_msg, w_text = await _poll_new_msg(baseline, timeout=5)
            last = w_msg.id
            log.info("[KEY-FLOW] /start OK (last_id=%d) welcome=%r", last, w_text[:60])
        except RuntimeError:
            # El bot no respondió al /start en 5s — capturar el último mensaje
            # disponible y continuar: el bot podría estar lento pero no caído
            try:
                fresh = await _client.get_messages(BOT_USERNAME, limit=1)
                last = fresh[0].id if fresh else baseline
            except Exception:
                pass
            log.warning(
                "[KEY-FLOW] /start sin respuesta en 5s — continuando con last_id=%d", last
            )

        await asyncio.sleep(1)

        await _client.send_message(BOT_USERNAME, key)
        k_msg, k_text = await _poll_new_msg(last, skip="checking", timeout=20)
        log.info("[KEY-FLOW] resultado key='%s'", k_text[:80])
        last = k_msg.id

        return last, k_text

    async def _do():
        async with _lock:
            # ── Intento 1: flujo normal ───────────────────────────────────────
            last_id, key_text = await _fresh_start_key_flow()

            # Bot estaba en estado 'esperando IP' — nuestra key fue tratada como IP
            if _bot_in_ip_state(key_text):
                log.warning(
                    "[KEY-FORCE] Bot en estado 'esperando IP' (key tomada como IP). "
                    "Completando flujo pendiente directamente con IP del usuario…"
                )
                # Completar el flujo pendiente enviando la IP real
                # (esto activa la key que quedó trabada y saca al bot del estado bloqueado)
                try:
                    last_id, _force_text = await _send_ip_and_confirm(last_id, "KEY-FORCE-A")
                    log.info("[KEY-FORCE] Flujo pendiente completado: '%s'", _force_text[:80])
                except Exception as exc:
                    log.warning("[KEY-FORCE] Error al completar flujo pendiente: %s — continuando…", exc)

                # ── Intento 2: ahora el bot debería estar libre — flujo real ──
                log.info("[KEY-FORCE] Rehaciendo flujo real para key actual…")
                last_id, key_text = await _fresh_start_key_flow()

                if _bot_in_ip_state(key_text):
                    raise RuntimeError(
                        "El bot de Telegram sigue bloqueado tras recovery. "
                        "Esperá unos minutos e intentá de nuevo."
                    )

                # Si la key muestra error DESPUÉS del force (p.ej. misma key ya activada)
                # → considerar éxito del flujo forzado (la key fue activada en el paso A)
                if any(kw in key_text.lower() for kw in _ERROR_KEYWORDS):
                    log.warning(
                        "[KEY-FORCE] Key inválida en flujo real post-force "
                        "(probablemente misma key activada en paso A). "
                        "Devolviendo resultado del paso A."
                    )
                    return _force_text

            # "network error" = error temporal del backend del bot proxy → reintentar
            # "invalid key" (sin network) = key realmente no existe → fallo permanente
            if "network" in key_text.lower():
                raise Exception(key_text)   # retryable por _run_with_retry
            if any(kw in key_text.lower() for kw in _ERROR_KEYWORDS):
                raise ValueError(key_text)

            # ── Paso 3 + 4: enviar IP y confirmar ────────────────────────────
            _last_id, result_text = await _send_ip_and_confirm(last_id, "KEY-IP")
            log.info("Telegram cmd_key respuesta final: %s", result_text[:120])
            return result_text

    return await _run_with_retry(_do, "cmd_key")


# ─────────────────────────────────────────────────────────────────────────────
# API pública async — bridgean Discord loop ↔ _tg_loop
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_gen(key: str, dias: int) -> str:
    """
    Llamar desde el contexto async de Discord.
    Ejecuta en _tg_loop sin bloquear el loop de Discord.
    """
    return await _submit(_cmd_gen_impl(key, dias))


async def cmd_key(key: str, ip: str) -> str:
    """
    Llamar desde el contexto async de Discord.
    Ejecuta en _tg_loop sin bloquear el loop de Discord.
    """
    return await _submit(_cmd_key_impl(key, ip))


# ─────────────────────────────────────────────────────────────────────────────
# Re-login interactivo vía Discord — genera nueva sesión desde producción
# ─────────────────────────────────────────────────────────────────────────────

_login_tmp_client: "TelegramClient | None" = None
_login_phone_hash: str | None = None
_login_phone:      str | None = None


async def _relogin_start_impl(phone: str) -> str:
    """Corre en _tg_loop. Crea cliente vacío y pide código SMS."""
    global _login_tmp_client, _login_phone_hash, _login_phone

    api_id   = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]

    if _login_tmp_client is not None:
        try:
            await _login_tmp_client.disconnect()
        except Exception:
            pass

    _login_tmp_client = TelegramClient(
        StringSession(),
        api_id,
        api_hash,
        proxy=None,
    )
    await _login_tmp_client.connect()

    result = await _login_tmp_client.send_code_request(phone)
    _login_phone_hash = result.phone_code_hash
    _login_phone      = phone

    log.info("Relogin: código enviado a %s.", phone)
    return (
        f"✅ Código enviado a `{phone}`.\n"
        f"Usá `/tg_codigo codigo:XXXXX` (sin espacios en el código).\n"
        f"Si tenés 2FA: `/tg_codigo codigo:XXXXX password:TuContraseña`"
    )


async def _relogin_verify_impl(code: str, password: str = "") -> str:
    """Corre en _tg_loop. Verifica código, guarda sesión nueva, reconecta."""
    global _client, _login_tmp_client, _login_phone_hash, _login_phone

    if _login_tmp_client is None or _login_phone_hash is None or _login_phone is None:
        raise RuntimeError("Primero ejecutá `/tg_relogin` para iniciar el proceso.")

    try:
        await _login_tmp_client.sign_in(
            phone=_login_phone,
            code=code,
            phone_code_hash=_login_phone_hash,
        )
    except SessionPasswordNeededError:
        if not password:
            raise RuntimeError(
                "⚠️ Esta cuenta tiene 2FA activo. "
                "Usá `/tg_codigo codigo:XXXXX password:TuContraseña`"
            )
        await _login_tmp_client.sign_in(password=password)
    except PhoneCodeInvalidError:
        raise RuntimeError("❌ Código incorrecto. Verificá bien el SMS y volvé a intentarlo.")

    new_session = _login_tmp_client.session.save()

    with open(_SESSION_FILE, "w", encoding="utf-8") as f:
        f.write(new_session)
    log.info("✅ Nueva sesión generada y guardada (largo=%d).", len(new_session))

    try:
        await _login_tmp_client.disconnect()
    except Exception:
        pass
    _login_tmp_client = None
    _login_phone_hash = None
    _login_phone      = None

    _ready.clear()
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:
            pass
    _client = None

    asyncio.ensure_future(_do_reconnect())

    return (
        f"✅ **Nueva sesión generada** (largo={len(new_session)}).\n\n"
        f"```\n{new_session}\n```\n\n"
        f"**Copiá ese texto** y actualizá el secret `TELEGRAM_SESSION` en Replit "
        f"con ese valor exacto, luego re-publicá el bot. "
        f"*(En dev ya funciona con el archivo local.)*"
    )


async def relogin_start(phone: str) -> str:
    """Llamar desde Discord. Inicia el flujo de re-autenticación."""
    return await _submit(_relogin_start_impl(phone))


async def relogin_verify(code: str, password: str = "") -> str:
    """Llamar desde Discord. Completa el login con el código SMS."""
    return await _submit(_relogin_verify_impl(code, password))
